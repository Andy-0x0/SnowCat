import os
import time
import winsound
from pathlib import Path
from dotenv import load_dotenv
from typing import List, Dict, Optional, Callable

import requests
from urllib.parse import urlparse, parse_qs
from playwright.sync_api import sync_playwright

from logger import Logger


class SnowCat:
    def __init__(
        self,
        level: str = 'INFO',
        config_path: os.PathLike | str = Path.cwd() / Path("../configs/.env.user_config")
    ) -> None:
        """
        Initialize a SnowCat (^=w=^) watching the course availability, and act accordingly if the course has a seat

        :param level:       the lowest printing level for the embedded logger
        :param config_path: the path of the user config file, which contains uiuc_portal_username and uiuc_portal_password
        """

        # init request config
        self.prefix = "https://banner.apps.uillinois.edu/StudentRegistrationSSB/ssb/searchResults/searchResults"
        self.refresh_prefix = "https://banner.apps.uillinois.edu/StudentRegistrationSSB/ssb/registration?mepCode=1UIUC#"
        self.params = {}
        self.headers = {}

       # init user config
        load_dotenv(config_path)
        self.username = os.getenv("UIUC_USERNAME")
        self.password = os.getenv("UIUC_PASSWORD")

        # init fetching status
        self.was_failed = False

        # init sync logger
        self.logger = Logger(level=level)

    def fetch(
        self,
        course_abb: str,
        course_num: str,
        course_ids: Optional[int | str | list[int | str] | tuple[int | str, ...] | set[int | str]] = None
    ) -> List[Dict]:
        """
        Execute the course information fetching

        :param course_abb: The abbreviation of the course (e.g. CS for Computer Science)
        :param course_num: The number of the course (e.g. 498 is the course number for CS498)
        :param course_ids: All course IDs that you are interested in (the 5-digit course id you can find in either the course explorer or the register portal)
        :return: A list of course config information, including the seat availability
        """

        self.params["txt_subject"] = course_abb
        self.params["txt_courseNumber"] = course_num

        response = requests.get(self.prefix, params=self.params, headers=self.headers)
        response = response.json()

        try:
            candidate_list = response["data"]
            if self.was_failed:
                self.logger.info(message="link reinitiated successfully", role="fetch")

            if course_ids is None:
                if "success" in response:
                    return candidate_list
                else:
                    self.logger.error(message=f"failed to initiate link", role="fetch")
                    raise ValueError(f"failed to fetch, state: {response['success']}")
            else:
                if isinstance(course_ids, (list, tuple, set)):
                    course_ids = list(str(ci) for ci in course_ids)
                else:
                    course_ids = [str(course_ids)]

                ret_list = [
                    candidate for candidate in candidate_list
                    if candidate["courseReferenceNumber"] in course_ids
                ]
                return ret_list

        except Exception as e:
            self.logger.error(message=f"failed to initiate link: {e}", role="fetch")
            raise ValueError(f"failed to fetch: {e}, state: {response['success']}")

    def _update_headers_from_request(self, req) -> None:
        """
        update the headers from the refreshed request to pass the request validation

        :param req: the request config form the refreshed request
        :return: None
        """

        if req is None:
            return
        self.headers = dict(req.all_headers())

    def _update_params_from_request(self, req) -> None:
        """
        update the params from the refreshed request to pass the request validation

        :param req: the request config form the refreshed request
        :return: None
        """

        if req is None:
            return
        qs = parse_qs(urlparse(req.url).query)
        if "uniqueSessionId" in qs and qs["uniqueSessionId"]:
            self.params["uniqueSessionId"] = qs["uniqueSessionId"][0]

    def refresh(
        self,
        course_field: str,
        course_num: int | str,
        timeout: int = 10*1000
    ) -> None:
        """
        Refreshes tokens such as cookies, headers, params, and sessions ... therefore the next request will stay valid

        :param course_field:    The full name of the course field (e.g. Computer Science is the course_field of CS)
        :param course_num:      The number of the course (e.g. 498 is the course number for CS498)
        :param timeout:         The time to wait for dom-wise-event to be noticed (in milliseconds)
        :return: None
        """

        self.logger.info(message="Token is expired, start fetching new token", role="refresh")

        with sync_playwright() as p:
            # 0) init driver
            browser = p.chromium.launch(headless=True)
            context = browser.new_context()
            page = context.new_page()
            page.set_default_timeout(timeout)

            # 1) open register page
            page.goto(self.refresh_prefix, wait_until="domcontentloaded")

            # 2) open the login page
            page.locator("#registerLink").click()

            # 3) send netid & password
            page.locator("#netid").fill(self.username)
            page.locator("#easpass").fill(self.password)
            page.locator('input[name="BTN_LOGIN"]').click()

            # 4) wait for DUO verification
            try_again_btn = page.locator("button.try-again-button")
            trust_btn = page.locator("#trust-browser-button")

            while True:
                if try_again_btn.is_visible() and (not trust_btn.is_visible()):
                    try_again_btn.first.click()
                    page.wait_for_load_state("domcontentloaded")
                    continue
                elif (not try_again_btn.is_visible()) and (not trust_btn.is_visible()):
                    page.wait_for_timeout(max(10, timeout // 10))
                elif (not try_again_btn.is_visible()) and trust_btn.is_visible():
                    trust_btn.click()
                    break

            # 6) select the term
            page.locator("#s2id_txt_term").click()
            page.locator("ul.select2-results li.select2-result-selectable").first.click()
            page.locator("#term-go").click()
            page.wait_for_load_state("networkidle")

            # 7) fill out subject + course number
            with page.expect_request(lambda r: "searchResults?txt_subject" in r.url) as req_info:
                subject = page.locator("#s2id_txt_subject")
                subject_input = subject.locator("li.select2-search-field input.select2-input")
                subject_input.fill(course_field)
                results = page.locator("ul.select2-results li.select2-result-selectable")
                results.first.wait_for(state="visible")

                while True:
                    if results.count() == 1:
                        results.first.click()
                        break
                    else:
                        page.wait_for_timeout(max(10, timeout // 100))
                        continue

                page.locator("#txt_courseNumber").fill(str(course_num))
                page.locator("#txt_courseNumber").press("Enter")
            req = req_info.value

            # 9) update the header & params for the refreshed request
            self._update_headers_from_request(req)
            self._update_params_from_request(req)

            context.close()
            browser.close()

            self.logger.info(message="Token successfully refreshed", role="refresh")

    def watch(
        self,
        course_field: str,
        course_abb: str,
        course_num: int | str,
        course_ids: Optional[int | str | list[int | str] | tuple[int | str, ...] | set[int | str]] = None,
        on_trigger: Optional[Callable] = None,
        interval: int = 15,
        timeout: int = 10*1000
    ) -> None:
        """
        The main method for watching the course availability + trigger specific actions when there is availability

        :param course_field:    The full name of the course field (e.g. Computer Science is the course_field of CS)
        :param course_abb:      The abbreviation of the course (e.g. CS for Computer Science)
        :param course_num:      The number of the course (e.g. 498 is the course number for CS498)
        :param course_ids:      All course IDs that you are interested in (the 5-digit course id you can find in either the course explorer or the register portal)
        :param on_trigger:      The function to call per success fetching
        :param interval:        The waiting time for a second course-availability-inquiry is initiated (in seconds)
        :param timeout:         The time to wait for dom-wise-event to be noticed (in milliseconds)
        :return: None
        """

        if on_trigger is None:
            def _(candidate_list, logger=self.logger):
                info_dict = {}
                alarm = False

                for candidate in candidate_list:
                    info_dict[candidate["courseReferenceNumber"]] = candidate['seatsAvailable']
                    if candidate["seatsAvailable"] > 0:
                        alarm = True

                if alarm:
                    logger.warning(role="SnowCat", message=f"Course {str(course_abb) + str(course_num)} available with spots: {info_dict}!")
                    for _ in range(5):
                        winsound.MessageBeep()
                        time.sleep(1.5)
                else:
                    logger.debug(role="SnowCat",message=f"Course {str(course_abb) + str(course_num)} available with spots: {info_dict}!")

            on_trigger = _

        while True:
            try:
                response_list = self.fetch(course_abb, course_num, course_ids)
                self.was_failed = False

            except Exception as e:
                if self.was_failed:
                    self.logger.error(role="SnowCat", message=f"Exit due to {str(e)}")
                    winsound.MessageBeep()
                    return
                else:
                    self.was_failed = True
                    self.refresh(course_field, course_num, timeout=timeout)
                    continue

            else:
                on_trigger(response_list, self.logger)

            time.sleep(interval)


if __name__ == "__main__":
    cat = SnowCat(level='DEBUG')
    cat.watch(
        course_field="Computer Science",
        course_abb="CS",
        course_num=421,
        course_ids=31375,
        on_trigger=None,
        interval=15,
        timeout=10*1000
    )


