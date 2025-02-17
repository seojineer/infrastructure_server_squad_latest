import hashlib
import logging
import re
import requests
import yaml
import json

from functools import reduce
from urllib.parse import urljoin

from squad.ci.backend.null import Backend as BaseBackend
from squad.ci.exceptions import FetchIssue, TemporaryFetchIssue


logger = logging.getLogger('squad.ci.backend.tuxsuite')


description = "TuxSuite"


class Backend(BaseBackend):

    """
    TuxSuite backend is intended for processing data coming from TuxTest
    """

    def generate_test_name(self, results):
        """
        Generates a name based on toolchain and config. Here are few examples:

        1) toolchain: gcc-9, kconfig: ['defconfig']
           -> returns 'gcc-9-defconfig'

        2) toolchain: gcc-9, kconfig: ['defconfig', 'CONFIG_LALA=y']
           -> returns 'gcc-9-defconfig-6bbfee93'
                                       -> hashlib.sha1('CONFIG_LALA=y')[0:8]

        3) toolchain: gcc-9, kconfig: ['defconfig', 'CONFIG_LALA=y', 'https://some.com/kconfig']
           -> returns 'gcc-9-defconfig-12345678'
                                      -> hashlib.sha1(
                                             sorted(
                                                 'CONFIG_LALA=y',
                                                 'https://some.com/kconfig',
                                             )
                                         )
        """
        name = results['toolchain']

        # If there are any configuration coming from a URL,
        # fetch it then merge all in a dictionary for later
        # hash it and make up the name
        configs = results['kconfig']
        name += f'-{configs[0]}'
        configs = configs[1:]

        if len(configs):
            sha = hashlib.sha1()

            for config in configs:
                sha.update(f'{config}'.encode())

            name += '-' + sha.hexdigest()[0:8]

        return name

    def parse_job_id(self, job_id):
        """
        Parsing the job id means getting back specific TuxSuite information
        from job_id. Ex:

        Given a job_id = "BUILD:linaro@anders#1yPYGaOEPNwr2pCqBgONY43zORq",
        the return value should be a tuple like

        ('BUILD', 'linaro@anders', '1yPYGaOEPNwr2pCqBgONY43zORq')

        """

        regex = r'^(BUILD|TEST):([0-9a-z_\-]+@[0-9a-z_\-]+)#([a-zA-Z0-9]+)$'
        matches = re.findall(regex, job_id)
        if len(matches) == 0:
            raise FetchIssue(f'Job id "{job_id}" does not match "{regex}"')

        # The regex below is supposed to find only one match
        return matches[0]

    def fetch_url(self, *urlbits):
        url = reduce(urljoin, urlbits)

        try:
            response = requests.get(url)
        except Exception as e:
            raise TemporaryFetchIssue(f"Can't retrieve from {url}: %s" % e)

        return response

    def parse_build_results(self, job_url, results, settings):
        required_keys = ['build_status', 'warnings_count', 'download_url', 'tuxmake_metadata']
        self.__check_required_keys__(required_keys, results)
        try:
            duration = results['tuxmake_metadata']['results']['duration']['build']
        except KeyError:
            raise FetchIssue('Missing duration from build results')

        # Make metadata
        metadata_keys = settings.get('BUILD_METADATA_KEYS', [])
        metadata = {k: results.get(k) for k in metadata_keys}
        metadata['job_url'] = job_url
        metadata['config'] = urljoin(results.get('download_url') + '/', 'config')

        # Generate generic test/metric name
        test_name = results.get('build_name') or self.generate_test_name(results)

        # Create tests
        tests = {}
        tests[f'build/{test_name}'] = results['build_status']

        # Create metrics
        metrics = {}
        metrics[f'build/{test_name}-duration'] = duration
        metrics[f'build/{test_name}-warnings'] = results['warnings_count']

        status = 'Complete'
        completed = True
        logs = self.fetch_url(results['download_url'], 'build.log').text
        return status, completed, metadata, tests, metrics, logs

    def parse_test_results(self, job_url, results, settings):
        status = 'Complete'
        completed = True

        # Pick up some metadata from results
        metadata_keys = settings.get('TEST_METADATA_KEYS', [])
        metadata = {k: results.get(k) for k in metadata_keys}
        metadata['job_url'] = job_url

        # Retrieve TuxRun log
        job_url += '/'
        logs = self.fetch_url(job_url, 'logs?format=txt').text

        # Really fetch test results
        tests = {}
        tests_results = self.fetch_url(job_url, 'results').json()
        for suite, suite_tests in tests_results.items():
            if suite == 'lava':
                continue

            suite_name = re.sub(r'^[0-9]+_', '', suite)
            for name, test_data in suite_tests.items():
                test_name = f'{suite_name}/{name}'
                result = test_data['result']

                # TODO: Log lines are off coming from TuxRun/LAVA
                # test_log = self.get_test_log(log_dict, test)
                tests[test_name] = result

        metrics = {}
        return status, completed, metadata, tests, metrics, logs

    def fetch(self, test_job):
        url = self.job_url(test_job)
        results = self.fetch_url(url).json()
        if results.get('state') != 'finished':
            return None

        settings = self.__resolve_settings__(test_job)

        result_type = self.parse_job_id(test_job.job_id)[0]
        parse_results = getattr(self, f'parse_{result_type.lower()}_results')
        return parse_results(url, results, settings)

    def job_url(self, test_job):
        result_type, tux_project, tux_uid = self.parse_job_id(test_job.job_id)
        tux_group, tux_user = tux_project.split('@')
        endpoint = f'groups/{tux_group}/projects/{tux_user}/{result_type.lower()}s/{tux_uid}'
        return urljoin(self.data.url, endpoint)

    def __check_required_keys__(self, required_keys, results):
        missing_keys = []
        for k in required_keys:
            if k not in results:
                missing_keys.append(k)

        if len(missing_keys):
            keys = ', '.join(missing_keys)
            results_json = json.dumps(results)
            raise FetchIssue(f'{keys} are required and missing from {results_json}')

    def __resolve_settings__(self, test_job):
        result_settings = self.settings
        if getattr(test_job, 'target', None) is not None \
                and test_job.target.project_settings is not None:
            ps = yaml.safe_load(test_job.target.project_settings) or {}
            result_settings.update(ps)
        return result_settings
