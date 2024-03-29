# Copyright (c) 2019, Palo Alto Networks
#
# Permission to use, copy, modify, and/or distribute this software for any
# purpose with or without fee is hereby granted, provided that the above
# copyright notice and this permission notice appear in all copies.
#
# THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
# WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
# MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR
# ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
# WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
# ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
# OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.

# Author: Giselle Serate <gserate@paloaltonetworks.com>

'''
Palo Alto Networks scraper.py

Downloads the latest release notes off a PANW firewall.

Don't run this file independently; intended as an include. Make sure to configure your .panrc.

This software is provided without support, warranty, or guarantee.
Use at your own risk.
'''

from enum import IntEnum, unique
import logging
import os
import re
from time import sleep

from elasticsearch_dsl import connections, Date, DocType, Integer, Keyword, Search, Text
from selenium import webdriver
from selenium.common.exceptions import (ElementClickInterceptedException, NoAlertPresentException,
                                        TimeoutException, UnexpectedAlertPresentException, WebDriverException)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait



@unique
class DocStatus(IntEnum):
    '''Defines document statuses.'''
    DOWNLOADED = 1
    PARSED = 2
    AUTOFOCUSED = 3



class VersionDocument(DocType):
    '''Contains update metadata.'''
    id = Text(analyzer='snowball', fields={'raw': Keyword()})
    shortversion = Text()
    version = Text()
    date = Date()
    status = Integer()


    class Index:
        '''Defines the index to send documents to.'''
        name = 'update-details'


    @classmethod
    def get_indexable(cls):
        '''Getter for objects.'''
        return cls.get_model().get_objects()


    @classmethod
    def from_obj(cls, obj):
        '''Convert to class.'''
        return cls(
            id=obj.id,
            shortversion=obj.shortversion,
            version=obj.version,
            date=obj.date,
            status=obj.status,
            )


    def save(self, **kwargs):
        return super(VersionDocument, self).save(**kwargs)



class FirewallScraper:
    '''
    A web scraping utility that downloads release notes from a firewall.
    Does NOT use elasticsearch.

    Non-keyword arguments:
    ip -- the IP of the firewall to scrape
    username -- the firewall username
    password -- the firewall password
    chrome_driver -- the name of the Chrome driver to use
    binary_location -- the path to the Chrome binary
    download_dir -- where to download the notes to

    '''
    def __init__(self, ip, username, password,
                 chrome_driver='chromedriver',
                 binary_location='/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary',
                 download_dir='contentpacks'):
        # Set up session details
        self._ip = ip
        self._username = username
        self._password = password

        # Set up driver
        chrome_options = Options()
        chrome_options.binary_location = binary_location
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        self._driver = webdriver.Chrome(executable_path=os.path.abspath(chrome_driver),
                                        options=chrome_options)

        # Init details
        self._download_dir = download_dir
        self.versions = []

        self._login()
        self._find_update_page()


    def __del__(self):
        self._driver.close()


    def _login(self):
        '''Log into firewall.'''
        # Load firewall login interface.
        self._driver.get(f'https://{self._ip}')

        # Fill login form and submit.
        user_box = self._driver.find_element_by_id('user')
        pwd_box = self._driver.find_element_by_id('passwd')
        user_box.clear()
        user_box.send_keys(self._username)
        pwd_box.clear()
        pwd_box.send_keys(self._password)
        pwd_box.send_keys(Keys.RETURN)

        timeout = 10

        # If the default creds box pops up, handle it.
        while True:
            timeout -= 1
            try:
                # Handle alert if we expect it to be there.
                if(self._username == 'admin' and self._password == 'admin'):
                    alert_box = self._driver.switch_to.alert
                    alert_box.accept()
                return
            except NoAlertPresentException:
                # We expect an alert, but haven't seen one yet.
                if timeout < 1:
                    return
                try:
                    # Firewall is not warning us about default creds, but might in a bit.
                    sleep(1)
                except UnexpectedAlertPresentException:
                    # Alert happened while we were sleeping; handle it.
                    alert_box = self._driver.switch_to.alert
                    alert_box.accept()
                    return


    def _find_update_page(self): # TODO: Sometimes we get stuck somewhere in this function. Fix it.
        '''Navigate to get the notes link and details.'''
        self._driver.get(f'https://{self._ip}')

        # Wait for page to load.
        timeout = 500
        try:
            device_tab_present = EC.presence_of_element_located((By.ID, 'device'))
            WebDriverWait(self._driver, timeout).until(device_tab_present)
        except TimeoutException:
            logging.error("Timed out waiting for post-login page to load.")
            raise TimeoutException

        # Go to device tab.
        device_tab = self._driver.find_element_by_id('device')
        device_tab.click()

        # Go to Dynamic Updates.
        dynamic_updates = self._driver.find_element_by_css_selector("div[ext\\3Atree-node-id='device/dynamic-updates']")
        dynamic_updates.click()

        # Get latest updates.
        check_now = self._driver.find_element_by_css_selector("table[itemid='Device/Dynamic Updates-Check Now']")
        self._driver.execute_script("arguments[0].scrollIntoView(true)", check_now)

        # Refresh update table
        while True:
            # Click as soon as the element is in view.
            while True:
                try:
                    check_now.click()
                    break
                except ElementClickInterceptedException:
                    sleep(1)
                except WebDriverException: # Alternate exception for Chromium in Docker
                    sleep(1)

            # Wait for updates to load in (otherwise we will get the old updates).
            sleep(10)

            # Wait for page to load.
            timeout = 30
            try:
                av_table_present = EC.presence_of_element_located((By.XPATH, "//div[contains (@id, '-gp-type-anti-virus-bd')]"))
                WebDriverWait(self._driver, timeout).until(av_table_present)
                break
            except TimeoutException:
                logging.warning('Timed out waiting for updates to load. Refresh again.')

        av_table = self._driver.find_element_by_xpath("//div[contains (@id, '-gp-type-anti-virus-bd')]") #'ext-gen468-gp-type-anti-virus-bd')
        av_children = av_table.find_elements_by_xpath('*')
        self.versions = []
        # Iterate all versions
        for child in av_children:
            source = child.get_attribute('innerHTML')
            # Iterate details of each version
            # Date should be formatted like 2019/06/14 04:02:07 PDT.
            date = re.search(r'[0-9]{4}\/[0-9]{2}\/[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2} PDT',
                             source).group(0)
            new_ver = {}
            new_ver['date'] = date
            # Version should be formatted like 3009-3519.
            new_ver['version'] = re.search(r'[0-9]{4}-[0-9]{4}', source).group(0)
            new_ver['link'] = re.search(r'https://downloads\.paloaltonetworks\.com/'
                                        r'virus/AntiVirusExternal-[0-9]*\.html'
                                        r'\?__gda__=[0-9]*_[a-z0-9]*', source).group(0)
            self.versions.append(new_ver)
        logging.debug(f"Discovered versions {self.versions}.")


    def _download_release(self, release):
        '''
        Download the specified release from the firewall and notate this in the database.

        Non-keyword arguments:
        release -- a dictionary containing details about the release to download
        '''
        logging.info(f"Downloading {release['version']} from firewall.")
        os.chdir(self._download_dir)
        self._driver.get(release['link'])
        filename = f"Updates_{release['version']}.html"
        with open(filename, 'w') as file:
            file.write(self._driver.page_source)


    def latest_download(self):
        '''Download the single latest release from the firewall.'''
        logging.info("Downloading the single latest release from the firewall.")
        latest = max(self.versions, key=lambda x: x['date'])
        self._download_release(latest)


    def all_available_download(self):
        '''Download all releases from the firewall.'''
        logging.info("Downloading all releases from the firewall.")
        for release in self.versions:
            self._download_release(release)



class ElasticFirewallScraper(FirewallScraper):
    '''
    A web scraping utility that downloads release notes from a firewall
    and writes this status to Elasticsearch. Allows downloading of only new releases.

    Non-keyword arguments:
    ip -- the IP of the firewall to scrape
    username -- the firewall username
    password -- the firewall password

    Keyword arguments: 
    chrome_driver -- the name of the Chrome driver to use
    binary_location -- the path to the Chrome binary
    download_dir -- where to download the notes to
    elastic_ip -- the IP of the database

    '''
    def __init__(self, ip, username, password,
                 chrome_driver='chromedriver',
                 binary_location='/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary',
                 download_dir='contentpacks', elastic_ip='localhost'):
        super(ElasticFirewallScraper, self).__init__(ip, username, password, chrome_driver,
                                                     binary_location, download_dir)
        self.num_new_releases = 0
        connections.create_connection(host=elastic_ip)


    def full_download(self):
        '''
        Download all undownloaded releases from the firewall.
        '''
        logging.info("Downloading all undownloaded releases from the firewall.")
        self.num_new_releases = 0
        for release in self.versions:
            version_search = (Search(index='update-details')
                              .query('match', version__keyword=release['version']))
            version_search.execute()
            downloaded = False
            for _ in version_search:
                downloaded = True
            if not downloaded:
                self._download_release(release)


    def latest_download(self):
        '''Download the page source of only the latest release notes.'''
        self.num_new_releases = 0
        super(ElasticFirewallScraper, self).latest_download()


    def all_available_download(self):
        '''Download the page source for all releases still on the firewall.'''
        self.num_new_releases = 0
        super(ElasticFirewallScraper, self).all_available_download()


    def _download_release(self, release):
        '''
        Download the specified release from the firewall and notate this in the database.

        Non-keyword arguments:
        release -- a dictionary containing details about the release to download
        '''
        super(ElasticFirewallScraper, self)._download_release(release)

        # Write version and date to elasticsearch
        version_doc = VersionDocument(meta={'id':release['version']})
        version_doc.shortversion = release['version'].split('-')[0]
        version_doc.version = release['version']
        version_doc.date = release['date']
        version_doc.status = DocStatus.DOWNLOADED.value
        version_doc.save()

        self.num_new_releases += 1
