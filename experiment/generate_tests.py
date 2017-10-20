#!/usr/bin/env python

# Copyright 2017 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Create e2e test definitions.

Usage example:

  In $GOPATH/src/k8s.io/test-infra,

  $ bazel run //experiment:generate_tests -- \
      --yaml-config-path=experiment/test_config.yaml \
      --json-config-path=jobs/config.json \
      --prow-config-path=prow/config.yaml

  After generating the tests, you should run:
  $ bazel run //jobs:config_sort
"""

import argparse
import hashlib
import json
import os
import ruamel.yaml as yaml


# TODO(yguo0905): Generate Prow and testgrid configurations.

PROW_CONFIG_TEMPLATE = """
    tags:
    - generated # AUTO-GENERATED by experiment/generate_tests.py - DO NOT EDIT!
    interval:
    agent: kubernetes
    name:
    spec:
      containers:
      - args:
        env:
        - name: GOOGLE_APPLICATION_CREDENTIALS
          value: /etc/service-account/service-account.json
        - name: USER
          value: prow
        - name: JENKINS_GCE_SSH_PRIVATE_KEY_FILE
          value: /etc/ssh-key-secret/ssh-private
        - name: JENKINS_GCE_SSH_PUBLIC_KEY_FILE
          value: /etc/ssh-key-secret/ssh-public
        image: gcr.io/k8s-testimages/kubekins-e2e:v20171018-fb8014bc-master
        volumeMounts:
        - mountPath: /etc/service-account
          name: service
          readOnly: true
        - mountPath: /etc/ssh-key-secret
          name: ssh
          readOnly: true
      volumes:
      - name: service
        secret:
          secretName: service-account
      - name: ssh
        secret:
          defaultMode: 256
          secretName: ssh-key-secret

"""

COMMENT = 'AUTO-GENERATED by experiment/generate_tests.py - DO NOT EDIT.'


def get_sha1_hash(data):
    """Returns the SHA1 hash of the specified data."""
    sha1_hash = hashlib.sha1()
    sha1_hash.update(data)
    return sha1_hash.hexdigest()


def substitute(job_name, lines):
    """Replace '${job_name_hash}' in lines with the SHA1 hash of job_name."""
    return [line.replace('${job_name_hash}', get_sha1_hash(job_name)[:10]) \
            for line in lines]


def get_envs(job_name, desc, field):
    """Returns a list of envs for the given field."""
    if not field or not field.get('envs', []):
        return []
    header = ['', '# The %s configurations.' % desc]
    return header + substitute(job_name, field.get('envs', []))


def get_args(job_name, field):
    """Returns a list of args for the given field."""
    if not field:
        return []
    return substitute(job_name, field.get('args', []))


def write_env_file(output_dir, job_name, envs):
    """Writes envs into a file in output_dir, and returns the file name."""
    output_file = os.path.join(output_dir, '%s.env' % job_name)
    if not envs:
        try:
            os.unlink(output_file)
        except OSError:
            pass
        return
    with open(output_file, 'w') as fp:
        fp.write('\n'.join(envs))
        fp.write('\n')


def write_job_defs_file(output_dir, job_defs):
    """Writes the job definitions into a file in output_dir."""
    output_file = os.path.join(output_dir, 'config.json')
    with open(output_file, 'w') as fp:
        json.dump(
            job_defs, fp, sort_keys=True, indent=2, separators=(',', ': '))
        fp.write('\n')


def write_prow_configs_file(output_dir, job_defs):
    """Writes the Prow configurations into a file in output_dir."""
    output_file = os.path.join(output_dir, 'config.yaml')
    with open(output_file, 'w') as fp:
        yaml.dump(
            job_defs, fp, Dumper=yaml.RoundTripDumper, width=float("inf"))
        fp.write('\n')


def apply_job_overrides(envs_or_args, job_envs_or_args):
    '''Applies the envs or args overrides defined in the job level'''
    for job_env_or_arg in job_envs_or_args:
        name = job_env_or_arg.split('=', 1)[0]
        env_or_arg = next(
            (x for x in envs_or_args if (x.strip().startswith('%s=' % name) or
                                         x.strip() == name)), None)
        if env_or_arg:
            envs_or_args.remove(env_or_arg)
        envs_or_args.append(job_env_or_arg)


class E2ENodeTest(object):

    def __init__(self, job_name, job, config):
        self.job_name = job_name
        self.job = job
        self.common = config['nodeCommon']
        self.images = config['nodeImages']
        self.k8s_versions = config['nodeK8sVersions']
        self.test_suites = config['nodeTestSuites']

    def __get_job_def(self, args):
        """Returns the job definition from the given args."""
        return {
            'scenario': 'kubernetes_e2e',
            'args': args,
            'sigOwners': self.job.get('sigOwners') or ['UNNOWN'],
            # Indicates that this job definition is auto-generated.
            'tags': ['generated'],
            '_comment': COMMENT,
        }

    def __get_prow_config(self, test_suite, k8s_version):
        """Returns the Prow config for the job from the given fields."""
        prow_config = yaml.round_trip_load(PROW_CONFIG_TEMPLATE)
        prow_config['name'] = self.job_name
        prow_config['interval'] = self.job['interval']
        # Assumes that the value in --timeout is of minutes.
        timeout = int(next(
            x[10:-1] for x in test_suite['args'] if (
                x.startswith('--timeout='))))
        container = prow_config['spec']['containers'][0]
        if not container['args']:
            container['args'] = []
        # Prow timeout = job timeout + 20min
        container['args'].append('--timeout=%d' % (timeout + 20))
        container['args'].extend(k8s_version)
        container['args'].append('--root=/go/src')
        container['env'].extend([{'name':'GOPATH', 'value': '/go'}])
        return prow_config

    def generate(self):
        '''Returns the job and the Prow configurations for this test.'''
        fields = self.job_name.split('-')
        if len(fields) != 6:
            raise ValueError('Expected 6 fields in job name', self.job_name)

        image = self.images[fields[3]]
        k8s_version = self.k8s_versions[fields[4][3:]]
        test_suite = self.test_suites[fields[5]]

        # envs are disallowed in node e2e tests.
        if 'envs' in self.common or 'envs' in image or 'envs' in test_suite:
            raise ValueError(
                'envs are disallowed in node e2e test', self.job_name)
        envs = []
        # Generates args.
        args = []
        args.extend(get_args(self.job_name, self.common))
        args.extend(get_args(self.job_name, image))
        args.extend(get_args(self.job_name, test_suite))
        # Generates job config.
        job_config = self.__get_job_def(args)
        # Generates prow config.
        prow_config = self.__get_prow_config(
            test_suite, get_args(self.job_name, k8s_version))

        # Combine --node-args
        node_args = []
        job_args = []
        for arg in job_config['args']:
            if '--node-args=' in arg:
                node_args.append(arg.split('=', 1)[1])
            else:
                job_args.append(arg)

        if node_args:
            flag = '--node-args='
            for node_arg in node_args:
                flag += '%s ' % node_arg
            job_args.append(flag.strip())

        job_config['args'] = job_args

        return envs, job_config, prow_config


class E2ETest(object):

    def __init__(self, output_dir, job_name, job, config):
        self.env_filename = os.path.join(output_dir, '%s.env' % job_name),
        self.job_name = job_name
        self.job = job
        self.common = config['common']
        self.cloud_providers = config['cloudProviders']
        self.images = config['images']
        self.k8s_versions = config['k8sVersions']
        self.test_suites = config['testSuites']

    def __get_job_def(self, args, envs):
        """Returns the job definition from the given args."""
        args += (['--env-file=%s' % self.env_filename] if envs else [])
        return {
            'scenario': 'kubernetes_e2e',
            'args': args,
            'sigOwners': self.job.get('sigOwners') or ['UNNOWN'],
            # Indicates that this job definition is auto-generated.
            'tags': ['generated'],
            '_comment': COMMENT,
        }

    def __get_prow_config(self, test_suite):
        """Returns the Prow config for the e2e job from the given fields."""
        prow_config = yaml.round_trip_load(PROW_CONFIG_TEMPLATE)
        prow_config['name'] = self.job_name
        prow_config['interval'] = self.job['interval']
        # Assumes that the value in --timeout is of minutes.
        timeout = int(next(
            x[10:-1] for x in test_suite['args'] if (
                x.startswith('--timeout='))))
        container = prow_config['spec']['containers'][0]
        if not container['args']:
            container['args'] = []
        container['args'].append('--bare')
        # Prow timeout = job timeout + 20min
        container['args'].append('--timeout=%d' % (timeout + 20))
        return prow_config

    def generate(self):
        '''Returns the job and the Prow configurations for this test.'''
        fields = self.job_name.split('-')
        if len(fields) != 7:
            raise ValueError('Expected 7 fields in job name', self.job_name)

        cloud_provider = self.cloud_providers[fields[3]]
        image = self.images[fields[4]]
        k8s_version = self.k8s_versions[fields[5][3:]]
        test_suite = self.test_suites[fields[6]]

        # Generates envs.
        envs = []
        envs.extend(get_envs(self.job_name, 'common', self.common))
        envs.extend(get_envs(self.job_name, 'cloud provider', cloud_provider))
        envs.extend(get_envs(self.job_name, 'image', image))
        envs.extend(get_envs(self.job_name, 'k8s version', k8s_version))
        envs.extend(get_envs(self.job_name, 'test suite', test_suite))
        # Generates args.
        args = []
        args.extend(get_args(self.job_name, self.common))
        args.extend(get_args(self.job_name, cloud_provider))
        args.extend(get_args(self.job_name, image))
        args.extend(get_args(self.job_name, k8s_version))
        args.extend(get_args(self.job_name, test_suite))
        # Generates job config.
        job_config = self.__get_job_def(args, envs)
        # Generates Prow config.
        prow_config = self.__get_prow_config(test_suite)

        return envs, job_config, prow_config


def for_each_job(output_dir, job_name, job, yaml_config):
    """Returns the job config and the Prow config for one test job."""
    fields = job_name.split('-')
    if len(fields) < 3:
        raise ValueError('Expected at least 3 fields in job name', job_name)
    job_type = fields[2]

    # Generates configurations.
    if job_type == 'e2e':
        generator = E2ETest(output_dir, job_name, job, yaml_config)
    elif job_type == 'e2enode':
        generator = E2ENodeTest(job_name, job, yaml_config)
    else:
        raise ValueError('Unexpected job type ', job_type)
    envs, job_config, prow_config = generator.generate()

    # Applies job-level overrides.
    if envs:
        envs.insert(0, '# ' + COMMENT)
    apply_job_overrides(envs, get_envs(job_name, 'job', job))
    apply_job_overrides(job_config['args'], get_args(job_name, job))

    # Writes the envs into the standalone file referenced in the job def.
    write_env_file(output_dir, job_name, envs)

    return job_config, prow_config


def remove_generated_jobs(json_config):
    """Removes all the generated job configs and their env files."""
    # TODO(yguo0905): Remove the generated env files as well.
    return {
        name: job_def for (name, job_def) in json_config.items()
        if 'generated' not in job_def.get('tags', [])}


def remove_generated_prow_configs(prow_config):
    """Removes all the generated Prow configurations."""
    # TODO(yguo0905): Handle non-periodics jobs.
    prow_config['periodics'] = [
        job for job in prow_config.get('periodics', [])
        if 'generated' not in job.get('tags', [])]


def main(json_config_path, yaml_config_path, prow_config_path, output_dir):
    """Creates test job definitions.

    Converts the test configurations in yaml_config_path to the job definitions
    in json_config_path and the env files in output_dir.
    """
    # TODO(yguo0905): Validate the configurations from yaml_config_path.

    with open(json_config_path) as fp:
        json_config = json.load(fp)
    json_config = remove_generated_jobs(json_config)

    with open(prow_config_path) as fp:
        prow_config = yaml.round_trip_load(fp, preserve_quotes=True)
    remove_generated_prow_configs(prow_config)

    with open(yaml_config_path) as fp:
        yaml_config = yaml.safe_load(fp)

    for job_name, _ in yaml_config['jobs'].items():
        # Get the envs and args for each job defined under "jobs".
        job, prow = for_each_job(
            output_dir, job_name, yaml_config['jobs'][job_name], yaml_config)
        json_config[job_name] = job
        prow_config['periodics'].append(prow)

    # Write the job definitions to config.json.
    write_job_defs_file(output_dir, json_config)
    write_prow_configs_file('prow', prow_config)


if __name__ == '__main__':
    PARSER = argparse.ArgumentParser(
        description='Create test definitions from the given yaml config')
    PARSER.add_argument('--yaml-config-path', help='Path to config.yaml')
    PARSER.add_argument('--json-config-path', help='Path to config.json')
    PARSER.add_argument('--prow-config-path', help='Path to the Prow config')
    PARSER.add_argument(
        '--output-dir', help='Env files output dir', default='jobs')
    ARGS = PARSER.parse_args()

    main(
        ARGS.json_config_path,
        ARGS.yaml_config_path,
        ARGS.prow_config_path,
        ARGS.output_dir)
