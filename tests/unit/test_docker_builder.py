import json
import os

import pytest

import docker_builder
from docker_builder import DockerBuilder, DockerError

HERE = os.path.dirname(os.path.abspath(__file__))
TESTS = os.path.dirname(HERE)
GOLDEN = os.path.join(TESTS, 'fixtures', 'dockerfiles')
FAKE_DOCKER = os.path.join(TESTS, 'support', 'fake_docker.py')

WORKLOADS = {
    'python': {'name': 'web-portal', 'runtime': 'python', 'port': 8080,
               'ports': [443, 80, 8080], 'entrypoint': 'gunicorn app:app'},
    'node': {'name': 'api-gateway', 'runtime': 'node', 'port': 3000,
             'entrypoint': 'node server.js'},
    'java': {'name': 'reports', 'runtime': 'java', 'ports': [8443, 8080]},
    'php': {'name': 'legacy-crm', 'runtime': 'php', 'port': 80},
}

INSPECT_JSON = json.dumps([{
    'Size': 126353408, 'Os': 'linux', 'Architecture': 'amd64',
    'RootFS': {'Layers': ['a', 'b', 'c', 'd', 'e']},
}])


class RecordingRunner(object):
    """Records every argv and answers with queued results (default success)."""

    def __init__(self, results=None):
        self.calls = []
        self.results = list(results or [])

    def run(self, argv, timeout=None):
        self.calls.append(list(argv))
        if self.results:
            return self.results.pop(0)
        return 0, '', ''


def read_golden(runtime):
    with open(os.path.join(GOLDEN, '{0}.Dockerfile'.format(runtime))) as f:
        return f.read()


@pytest.mark.parametrize('runtime', ['python', 'node', 'java', 'php'])
def test_generated_dockerfile_matches_golden(runtime):
    builder = DockerBuilder({})
    assert builder.generate_dockerfile(WORKLOADS[runtime]) == read_golden(runtime)


def test_entrypoint_is_split_into_exec_form():
    builder = DockerBuilder({})
    text = builder.generate_dockerfile(WORKLOADS['python'])
    assert 'CMD ["gunicorn", "app:app"]' in text.splitlines()


def test_quoted_entrypoint_keeps_quoted_argument_together():
    builder = DockerBuilder({})
    w = dict(WORKLOADS['python'], entrypoint='sh -c "exec gunicorn app:app"')
    lines = builder.generate_dockerfile(w).splitlines()
    assert 'CMD ["sh", "-c", "exec gunicorn app:app"]' in lines


def test_unknown_runtime_falls_back_to_ubuntu_and_default_port():
    builder = DockerBuilder({})
    lines = builder.generate_dockerfile({'name': 'batch', 'runtime': 'go'}).splitlines()
    assert lines[0] == 'FROM ubuntu:14.04'
    assert 'EXPOSE 8080' in lines


def test_base_images_override_from_config():
    builder = DockerBuilder({'base_images': {'python': 'python:2.7-slim'}})
    lines = builder.generate_dockerfile(WORKLOADS['python']).splitlines()
    assert lines[0] == 'FROM python:2.7-slim'
    node_lines = builder.generate_dockerfile(WORKLOADS['node']).splitlines()
    assert node_lines[0] == 'FROM node:0.12-slim'


def test_build_tag_push_argv_sequence(tmpdir):
    runner = RecordingRunner()
    builder = DockerBuilder({'registry': 'registry.local:5000'}, runner=runner)
    w = {'name': 'web-portal', 'runtime': 'python', 'version': '1.0',
         'source_path': str(tmpdir)}
    dockerfile = os.path.join(str(tmpdir), 'Dockerfile')

    assert builder.build_image(w) == 'web-portal:1.0'
    assert builder.push(w) == 'registry.local:5000/web-portal:1.0'

    assert runner.calls == [
        ['docker', 'build', '-t', 'web-portal:1.0', '-f', dockerfile, str(tmpdir)],
        ['docker', 'tag', 'web-portal:1.0', 'registry.local:5000/web-portal:1.0'],
        ['docker', 'push', 'registry.local:5000/web-portal:1.0'],
    ]
    with open(dockerfile) as f:
        assert f.read().startswith('FROM python:3.4-slim\n')


def test_push_to_ecr_name_is_gone():
    assert not hasattr(DockerBuilder, 'push_to_ecr')


def test_command_comes_from_config():
    runner = RecordingRunner()
    builder = DockerBuilder({'command': '/usr/local/bin/docker', 'registry': 'r:5000'},
                            runner=runner)
    builder.push({'name': 'api', 'version': '2'})
    assert [c[0] for c in runner.calls] == ['/usr/local/bin/docker'] * 2


def test_existing_dockerfile_is_not_overwritten(tmpdir):
    existing = tmpdir.join('Dockerfile')
    existing.write('FROM scratch\n')
    runner = RecordingRunner()
    builder = DockerBuilder({}, runner=runner)
    builder.build_image({'name': 'app', 'runtime': 'python', 'source_path': str(tmpdir)})
    assert existing.read() == 'FROM scratch\n'
    assert runner.calls[0][:4] == ['docker', 'build', '-t', 'app:latest']


def test_build_failure_raises_docker_error_with_stderr(tmpdir):
    runner = RecordingRunner([(1, '', 'no space left on device')])
    builder = DockerBuilder({}, runner=runner)
    with pytest.raises(DockerError) as excinfo:
        builder.build_image({'name': 'app', 'source_path': str(tmpdir)})
    assert excinfo.value.stderr == 'no space left on device'
    assert excinfo.value.returncode == 1
    assert 'no space left on device' in str(excinfo.value)


def test_push_failure_stops_after_failing_step():
    runner = RecordingRunner([(0, '', ''), (1, '', 'denied: requested access')])
    builder = DockerBuilder({'registry': 'registry.local:5000'}, runner=runner)
    with pytest.raises(DockerError) as excinfo:
        builder.push({'name': 'web-portal', 'version': '1.0'})
    assert excinfo.value.stderr == 'denied: requested access'
    assert excinfo.value.argv[1] == 'push'
    assert len(runner.calls) == 2


def test_push_without_registry_raises():
    runner = RecordingRunner()
    builder = DockerBuilder({}, runner=runner)
    with pytest.raises(DockerError):
        builder.push({'name': 'web-portal'})
    assert runner.calls == []


def test_analyze_image_summarises_inspect_output():
    runner = RecordingRunner([(0, INSPECT_JSON, '')])
    builder = DockerBuilder({}, runner=runner)
    assert builder.analyze_image('web-portal:1.0') == {
        'size_mb': 120.5, 'layers': 5, 'os': 'linux', 'arch': 'amd64'}
    assert runner.calls == [['docker', 'inspect', 'web-portal:1.0']]


def test_analyze_image_failure_raises_docker_error():
    runner = RecordingRunner([(1, '', 'No such image: ghost')])
    builder = DockerBuilder({}, runner=runner)
    with pytest.raises(DockerError) as excinfo:
        builder.analyze_image('ghost')
    assert excinfo.value.stderr == 'No such image: ghost'


def test_real_command_runner_drives_fake_docker(tmpdir, monkeypatch):
    log = tmpdir.join('docker.log')
    monkeypatch.setenv('FAKE_DOCKER_LOG', str(log))
    builder = DockerBuilder({'command': FAKE_DOCKER, 'registry': 'registry.local:5000'},
                            runner=docker_builder.CommandRunner())
    src = tmpdir.mkdir('src')
    w = {'name': 'web-portal', 'runtime': 'python', 'version': '1.0',
         'source_path': str(src)}

    builder.build_image(w)
    builder.push(w)
    summary = builder.analyze_image('web-portal:1.0')

    lines = [json.loads(line) for line in log.read().splitlines()]
    assert lines == [
        ['build', '-t', 'web-portal:1.0', '-f', str(src.join('Dockerfile')), str(src)],
        ['tag', 'web-portal:1.0', 'registry.local:5000/web-portal:1.0'],
        ['push', 'registry.local:5000/web-portal:1.0'],
        ['inspect', 'web-portal:1.0'],
    ]
    assert summary == {'size_mb': 120.5, 'layers': 5, 'os': 'linux', 'arch': 'amd64'}

    with pytest.raises(DockerError) as excinfo:
        builder.analyze_image('FAIL')
    assert 'forced failure' in excinfo.value.stderr
