import sys
from unittest import mock

import pytest

import docker_builder


def test_command_runner_returns_code_and_output():
    runner = docker_builder.CommandRunner()
    code, out, err = runner.run(
        [sys.executable, '-c', 'import sys; print("hi"); sys.stderr.write("oops"); sys.exit(3)'])
    assert code == 3
    assert out.strip() == 'hi'
    assert err == 'oops'


def test_build_image_routes_through_runner(tmpdir):
    runner = mock.MagicMock()
    runner.run.return_value = (0, '', '')
    builder = docker_builder.DockerBuilder({}, runner=runner)
    name = builder.build_image({'name': 'portal', 'version': '1', 'source_path': str(tmpdir)})
    assert name == 'portal:1'
    argv = runner.run.call_args[0][0]
    assert argv[:4] == ['docker', 'build', '-t', 'portal:1']


def test_push_and_analyze_route_through_runner():
    runner = mock.MagicMock()
    runner.run.side_effect = [
        (0, '', ''), (0, '', ''),
        (0, '[{"Size": 2097152, "RootFS": {"Layers": [1, 2]}, "Os": "linux",'
            ' "Architecture": "amd64"}]', ''),
    ]
    builder = docker_builder.DockerBuilder({'registry': 'reg:5000'}, runner=runner)
    assert builder.push({'name': 'portal'}) == 'reg:5000/portal:latest'
    assert builder.analyze_image('portal:latest') == {
        'size_mb': 2.0, 'layers': 2, 'os': 'linux', 'arch': 'amd64'}
    argvs = [c[0][0] for c in runner.run.call_args_list]
    assert argvs[0] == ['docker', 'tag', 'portal:latest', 'reg:5000/portal:latest']
    assert argvs[1] == ['docker', 'push', 'reg:5000/portal:latest']
    assert argvs[2] == ['docker', 'inspect', 'portal:latest']


def test_push_failure_raises_docker_error():
    runner = mock.MagicMock()
    runner.run.side_effect = [(0, '', ''), (1, '', 'denied')]
    builder = docker_builder.DockerBuilder({'registry': 'reg:5000'}, runner=runner)
    with pytest.raises(docker_builder.DockerError):
        builder.push({'name': 'portal'})
