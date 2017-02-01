# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import ConfigParser
import csv
import json
import os
import re
import time
import urllib
import urllib2
import urlparse
from math import sqrt

from jot import jwt, jws

import utils
from build_dates import TIMESTAMP, convert_datetime_to_string
from phonetest import PhoneTest

# PerfherderArtifact and PerfherderSuite are specific formats for
# Perfherder as defined in:
# https://bugzilla.mozilla.org/show_bug.cgi?id=1175295

# These work with summarized suite values and summarized test value,
# instead of working with raw replicates.

# In the future it would be nice to have a generic PerfData class which
# stores the raw replicates and then the output functions can do the final
# summarization and calculations as defined by the output medium (e.g. perfherder)

class PerfherderArtifact(dict):
    def __init__(self, suites=None):
        super(PerfherderArtifact, self).__init__(self)
        self["framework"] = {'name': 'autophone'}
        if suites is None:
            suites = []
        self["suites"] = suites

    def add_suite(self, suite):
        self["suites"].append(suite)


class PerfherderSuite(dict):

    def __init__(self, name=None, value=0, subtests=None, options=None):
        super(PerfherderSuite, self).__init__(self)
        if subtests is None:
            self['subtests'] = []
        else:
            self['subtests'] = subtests
        self['name'] = name
        self['value'] = value
        if options:
            self.update(options)

    def add_subtest(self, name, value, options=None):
        subtest = {'name': name, 'value': value}
        if options:
            subtest.update(options)
        self['subtests'].append(subtest)


class PerfherderOptions(dict):

    def __init__(self, options, repo=None):
        super(PerfherderOptions, self).__init__(self)
        self.update(options)
        if repo and repo in 'mozilla-beta,mozilla-release':
            # Adjust the windows to half of the normal size for
            # mozilla-beta and mozilla-release which do not have as
            # many builds as other repos.
            if 'min_back_window' in self:
                self['min_back_window'] = self['min_back_window'] / 2
            if 'max_back_window' in self:
                self['max_back_window'] = self['max_back_window'] / 2
            if 'fore_window' in self:
                self['fore_window'] = self['fore_window'] / 2


class PerfTest(PhoneTest):
    def __init__(self, dm=None, phone=None, options=None,
                 config_file=None, chunk=1, repos=[]):
        PhoneTest.__init__(self, dm=dm, phone=phone, options=options,
                           config_file=config_file, chunk=chunk, repos=repos)
        self._result_server = None
        self._resulturl = None
        self.perfherder_artifact = None
        if options.phonedash_url:
            self._resulturl = urlparse.urljoin(options.phonedash_url, '/api/s1s2/')
            self.loggerdeco.debug('PerfTest._resulturl: %s' % self._resulturl)

        # [signature]
        self._signer = None
        self._jwt = {'id': options.phonedash_user, 'key': options.phonedash_password}
        # phonedash requires both an id and a key.
        if self._jwt['id'] and self._jwt['key']:
            self._signer = jws.HmacSha(key=self._jwt['key'],
                                       key_id=self._jwt['id'])
        # [settings]
        try:
            self._iterations = self.cfg.getint('settings', 'iterations')
        except (ConfigParser.NoSectionError, ConfigParser.NoOptionError):
            self._iterations = 1
        try:
            self.stderrp_accept = self.cfg.getfloat('settings', 'stderrp_accept')
        except (ConfigParser.NoSectionError, ConfigParser.NoOptionError):
            self.stderrp_accept = 0
        try:
            self.stderrp_reject = self.cfg.getfloat('settings', 'stderrp_reject')
        except (ConfigParser.NoSectionError, ConfigParser.NoOptionError):
            self.stderrp_reject = 100
        try:
            self.stderrp_attempts = self.cfg.getint('settings', 'stderrp_attempts')
        except (ConfigParser.NoSectionError, ConfigParser.NoOptionError):
            self.stderrp_attempts = 1
        self.perfherder_options = {}
        try:
            self.perfherder_options['alert_threshold'] = self.cfg.getint('perfherder',
                                                                         'alert_threshold')
        except (ConfigParser.NoSectionError, ConfigParser.NoOptionError):
            pass
        try:
            self.perfherder_options['min_back_window'] = self.cfg.getint('perfherder',
                                                                         'min_back_window')
        except (ConfigParser.NoSectionError, ConfigParser.NoOptionError):
            pass
        try:
            self.perfherder_options['max_back_window'] = self.cfg.getint('perfherder',
                                                                         'max_back_window')
        except (ConfigParser.NoSectionError, ConfigParser.NoOptionError):
            pass
        try:
            self.perfherder_options['fore_window'] = self.cfg.getint('perfherder', 'fore_window')
        except (ConfigParser.NoSectionError, ConfigParser.NoOptionError):
            pass
        self._resultfile = None
        self._resultwriter = None

        self.loggerdeco.debug('PerfTest: %s', self.__dict__)

    def setup_job(self):
        PhoneTest.setup_job(self)
        self.perfherder_artifact = None

        if not self._resulturl:
            self._resultfile = open('autophone-results-%s.csv' %
                                    self.phone.id, 'ab')
            self._resultfile.seek(0, 2)
            self._resultwriter = csv.writer(self._resultfile)
            if self._resultfile.tell() == 0:
                self._resultwriter.writerow([
                    'phoneid',
                    'testname',
                    'starttime',
                    'throbberstartraw',
                    'throbberstopraw',
                    'throbberstart',
                    'throbberstop',
                    'blddate',
                    'cached',
                    'rejected',
                    'revision',
                    'productname',
                    'productversion',
                    'osver',
                    'bldtype',
                    'machineid'])

    def _phonedash_url(self, testname):
        # Map the config filename to the test name used on phonedash.
        # This depends on the config file naming scheme for the s1s2
        # test configuration files following this pattern.
        re_test = re.compile(r's1s2-([^-]+)-(.*)[.]ini')
        config = os.path.basename(self.config_file)
        match = re_test.match(config)
        if match:
            test_name = match.group(2) + '-' + match.group(1)
        if not self.result_server or not self.build or not match:
            return 'http://phonedash.mozilla.org/'
        buildday = (self.build.id[0:4] + '-' + self.build.id[4:6] + '-' +
                    self.build.id[6:8])
        url = ('%s/#/%s/%s/'
               'binning=repo-phonetype-phoneid-test_name-cached_label-metric&'
               '%s=on&%s=on&'
               '%s=on&'
               'throbberstart=on&throbberstop=on&first=on&second=on&'
               'rejected=norejected&'
               'errorbars=errorbars&errorbartype=standarderror&'
               'valuetype=median' % (
                   self.result_server, buildday, buildday,
                   self.build.tree, self.phone.id, test_name))
        return url

    @property
    def result_server(self):
        if self._resulturl and not self._result_server:
            parts = urlparse.urlparse(self._resulturl)
            self._result_server = '%s://%s' % (parts.scheme, parts.netloc)
            self.loggerdeco.debug('PerfTest._result_server: %s' % self._result_server)
        return self._result_server

    @property
    def phonedash_url(self):
        raise NotImplementedError

    def teardown_job(self):
        self.loggerdeco.debug('PerfTest.teardown_job')

        if self._resultfile:
            self._resultfile.close()
            self._resultfile = None

        PhoneTest.teardown_job(self)
        self.perfherder_artifact = None

    def report_results(self, starttime=0, tstrt=0, tstop=0,
                       testname='', cache_enabled=True,
                       rejected=False):
        msg = ('Tree: %s Cached: %s '
               'Start Time: %s Throbber Start Raw: %s Throbber Stop Raw: %s '
               'Throbber Start: %s Throbber Stop: %s '
               'Total Throbber Time: %s Rejected: %s' % (
                   self.build.tree, cache_enabled,
                   starttime, tstrt, tstop,
                   tstrt-starttime, tstop-starttime,
                   tstop - tstrt, rejected))
        self.loggerdeco.info('RESULTS: %s' % msg)

        if self._resulturl:
            self.publish_results(starttime=starttime, tstrt=tstrt, tstop=tstop,
                                 testname=testname, cache_enabled=cache_enabled,
                                 rejected=rejected)
        else:
            self.dump_results(starttime=starttime, tstrt=tstrt, tstop=tstop,
                              testname=testname, cache_enabled=cache_enabled,
                              rejected=rejected)

    def publish_results(self, starttime=0, tstrt=0, tstop=0,
                        testname='', cache_enabled=True,
                        rejected=False):
        # Create JSON to send to webserver
        author = None
        if self.build.tree == 'try':
            rev_json_url = self.build.changeset.replace('/rev/', '/json-rev/')
            rev_json = utils.get_remote_json(rev_json_url)
            if rev_json:
                author = rev_json['pushuser']

        blddate = float(convert_datetime_to_string(self.build.date, TIMESTAMP))
        self.loggerdeco.debug('publish_results: build.id: %s, build.date: %s, blddate: %s' % (
            self.build.id, self.build.date, blddate))

        resultdata = {
            'phoneid': self.phone.id,
            'testname': testname,
            'starttime': starttime,
            'throbberstart': tstrt,
            'throbberstop': tstop,
            'blddate': blddate,
            'cached': cache_enabled,
            'rejected': rejected,
            'revision': self.build.changeset,
            'author': author,
            'productname': self.build.app_name,
            'productversion': self.build.version,
            'osver': self.phone.osver,
            'bldtype': self.build.type,
            'machineid': self.phone.machinetype
        }

        result = {'data': resultdata}
        # Upload
        if self._signer:
            encoded_result = jwt.encode(result, signer=self._signer)
            content_type = 'application/jwt'
        else:
            encoded_result = json.dumps(result)
            content_type = 'application/json; charset=utf-8'
        req = urllib2.Request(self._resulturl + 'add/', encoded_result,
                              {'Content-Type': content_type})
        max_attempts = 10
        wait_time = 10
        for attempt in range(1, max_attempts+1):
            try:
                f = urllib2.urlopen(req)
                f.read()
                f.close()
                return
            except Exception, e:
                # Retry submission if the exception is due to a
                # timeout and if we haven't exceeded the maximum
                # number of attempts.
                if attempt < max_attempts:
                    self.loggerdeco.warning('PerfTest.publish_results: '
                                            'Attempt %d/%d error %s sending '
                                            'results to server' % (
                                                attempt, max_attempts,
                                                e))
                    time.sleep(wait_time)
                    continue
                self.loggerdeco.exception('Error sending results to server')
                self.worker_subprocess.mailer.send(
                    '%s attempt %s/%s Error sending %s results for phone %s, '
                    'build %s' % (utils.host(), attempt, max_attempts,
                                  self.name, self.phone.id, self.build.id),
                    'There was an error attempting to send test results '
                    'to the result server %s.\n'
                    '\n'
                    'Host       %s\n'
                    'Job        %s\n'
                    'Test       %s\n'
                    'Phone      %s\n'
                    'Repository %s\n'
                    'Build      %s\n'
                    'Revision   %s\n'
                    'Exception  %s\n'
                    'Result     %s\n' %
                    (self.result_server,
                     utils.host(),
                     self.job_url,
                     self.name,
                     self.phone.id,
                     self.build.tree,
                     self.build.id,
                     self.build.changeset,
                     e,
                     json.dumps(resultdata, sort_keys=True, indent=2)))
                message = 'Error sending results to server'
                self.status = PhoneTest.EXCEPTION
                self.message = message
                self.update_status(message=message)

    def dump_results(self, starttime=0, tstrt=0, tstop=0,
                     testname='', cache_enabled=True,
                     rejected=False):
        self._resultwriter.writerow([
            self.phone.id,
            testname,
            starttime,
            tstrt,
            tstop,
            tstrt-starttime,
            tstop-starttime,
            self.build.date,
            cache_enabled,
            rejected,
            self.build.changeset,
            self.build.app_name,
            self.build.version,
            self.phone.osver,
            self.build.type,
            self.phone.machinetype])

    def check_results(self, testname=''):
        """Return True if there already exist unrejected results for this device,
        build and test.
        """

        if not self._resulturl:
            return False

        # Create JSON to send to webserver
        query = {
            'phoneid': self.phone.id,
            'test': testname,
            'revision': self.build.changeset,
            'product': self.build.app_name
        }

        self.loggerdeco.debug('check_results for: %s' % query)

        url = self._resulturl + 'check/?' + urllib.urlencode(query)
        response = utils.get_remote_json(url)
        self.loggerdeco.debug('check_results: content: %s' % response)
        if response:
            return response['result']

        self.loggerdeco.warning(
            'check_results: could not check: '
            'phoneid: %s, test: %s, revision: %s, product: %s' % (
                query['phoneid'], query['test'],
                query['revision'], query['product']))
        return False

    def get_stats(self, values):
        """Calculate and return an object containing the count, mean,
        standard deviation, standard error of the mean and percentage
        standard error of the mean of the values list."""
        r = {'count': len(values)}
        if r['count'] == 1:
            r['mean'] = values[0]
            r['stddev'] = 0
            r['stderr'] = 0
            r['stderrp'] = 0
        else:
            r['mean'] = sum(values) / float(r['count'])
            r['stddev'] = sqrt(sum([(value - r['mean'])**2
                                    for value in values])/float(r['count']-1.5))
            r['stderr'] = r['stddev']/sqrt(r['count'])
            r['stderrp'] = 100.0*r['stderr']/float(r['mean'])
        return r

    def is_stderr_below_threshold(self, measurements, dataset, threshold):
        """Return True if all of the measurements in the dataset have
        standard errors of the mean below the threshold.

        Return False if at least one measurement is above the threshold
        or if one or more datasets have only one value.

        Return None if at least one measurement has no values.
        """

        self.loggerdeco.debug("is_stderr_below_threshold: %s" % dataset)

        for cachekey in ('uncached', 'cached'):
            for measurement in measurements:
                data = [datapoint[cachekey][measurement] - datapoint[cachekey]['starttime']
                        for datapoint in dataset
                        if datapoint and cachekey in datapoint]
                if not data:
                    return None
                stats = self.get_stats(data)
                self.loggerdeco.debug('%s %s count: %d, mean: %.2f, '
                                      'stddev: %.2f, stderr: %.2f, '
                                      'stderrp: %.2f' % (
                                          cachekey, measurement,
                                          stats['count'], stats['mean'],
                                          stats['stddev'], stats['stderr'],
                                          stats['stderrp']))
                if stats['count'] == 1 or stats['stderrp'] >= threshold:
                    return False
        return True
