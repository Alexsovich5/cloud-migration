"""
Docker Builder Module

Containerizes workloads for migration: generates a Dockerfile per runtime,
builds the image, tags and pushes it to a private registry, and summarises
``docker inspect`` output. Every docker invocation goes through an injectable
command runner so it can be recorded or faked.
"""

import json
import logging
import os
import shlex
import subprocess

logger = logging.getLogger('docker_builder')

DEFAULT_BASE_IMAGES = {
    'python': 'python:3.4-slim',
    'node': 'node:0.12-slim',
    'java': 'java:8u45-jre',
    'php': 'php:5.6-apache',
}
FALLBACK_BASE_IMAGE = 'ubuntu:14.04'
DEFAULT_PORT = 8080
BUILD_TIMEOUT = 600


class DockerError(Exception):
    """A docker command exited non-zero."""

    def __init__(self, message, argv=None, returncode=None, stderr=''):
        super(DockerError, self).__init__(message)
        self.argv = argv
        self.returncode = returncode
        self.stderr = stderr


class CommandRunner:
    """Runs external commands and captures their output as text."""

    def run(self, argv, timeout=None):
        """Run argv and return (returncode, stdout, stderr).

        If the command exceeds timeout seconds it is killed and
        subprocess.TimeoutExpired is raised.
        """
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise
        return proc.returncode, stdout, stderr


def _expose_ports(workload):
    ports = set()
    for port in workload.get('ports') or []:
        ports.add(int(port))
    if workload.get('port') is not None:
        ports.add(int(workload['port']))
    if not ports:
        ports.add(DEFAULT_PORT)
    return sorted(ports)


def _cmd_argv(workload, runtime):
    entrypoint = workload.get('entrypoint', '')
    if entrypoint:
        return shlex.split(entrypoint)
    if runtime == 'python':
        return ['python', 'app.py']
    if runtime == 'java':
        return ['java', '-jar', 'app.jar']
    return None


class DockerBuilder:
    """Manages Docker containerization for migration workloads."""

    def __init__(self, config, runner=None):
        config = config or {}
        self.runner = runner if runner is not None else CommandRunner()
        self.command = config.get('command') or 'docker'
        self.registry = config.get('registry', '')
        self.base_images = dict(DEFAULT_BASE_IMAGES)
        self.base_images.update(config.get('base_images') or {})

    def _run(self, args, timeout=None):
        argv = [self.command] + list(args)
        returncode, stdout, stderr = self.runner.run(argv, timeout=timeout)
        if returncode != 0:
            logger.error("%s failed (%s): %s", ' '.join(argv), returncode, stderr)
            raise DockerError(
                "docker {0} exited {1}: {2}".format(args[0], returncode, (stderr or '').strip()),
                argv=argv, returncode=returncode, stderr=stderr)
        return stdout

    def generate_dockerfile(self, workload):
        """Generate a Dockerfile based on workload analysis."""
        runtime = workload.get('runtime', 'python')
        base = self.base_images.get(runtime, FALLBACK_BASE_IMAGE)

        lines = [
            "FROM {0}".format(base),
            "MAINTAINER migration-framework",
            "LABEL project=\"{0}\"".format(workload['name']),
            "",
        ]

        if runtime == 'php':
            lines.append("COPY . /var/www/html/")
        else:
            lines.extend(["WORKDIR /app", ""])
            if runtime == 'python':
                lines.extend([
                    "COPY requirements.txt /app/",
                    "RUN pip install --no-cache-dir -r requirements.txt",
                    "",
                ])
            elif runtime == 'node':
                lines.extend([
                    "COPY package.json /app/",
                    "RUN npm install --production",
                    "",
                ])
            elif runtime == 'java':
                lines.extend(["COPY target/*.jar /app/app.jar", ""])
            lines.append("COPY . /app")

        lines.extend([
            "",
            "EXPOSE {0}".format(' '.join(str(p) for p in _expose_ports(workload))),
        ])

        cmd = _cmd_argv(workload, runtime)
        if cmd:
            lines.extend(["", "CMD {0}".format(json.dumps(cmd))])

        return '\n'.join(lines) + '\n'

    @staticmethod
    def image_name(workload):
        return "{0}:{1}".format(workload['name'], workload.get('version', 'latest'))

    def build_image(self, workload):
        """Build the workload image, generating a Dockerfile if none exists."""
        image_name = self.image_name(workload)
        context = workload.get('source_path', '.')
        dockerfile_path = os.path.join(context, 'Dockerfile')

        if not os.path.exists(dockerfile_path):
            with open(dockerfile_path, 'w') as f:
                f.write(self.generate_dockerfile(workload))
            logger.info("Generated Dockerfile at %s", dockerfile_path)

        logger.info("Building Docker image: %s", image_name)
        self._run(['build', '-t', image_name, '-f', dockerfile_path, context],
                  timeout=BUILD_TIMEOUT)
        logger.info("Successfully built %s", image_name)
        return image_name

    def push(self, workload):
        """Tag the local image for the registry and push it."""
        if not self.registry:
            raise DockerError("no docker registry configured")
        local_image = self.image_name(workload)
        remote_image = "{0}/{1}".format(self.registry.rstrip('/'), local_image)

        logger.info("Pushing %s to %s", local_image, remote_image)
        self._run(['tag', local_image, remote_image])
        self._run(['push', remote_image])
        logger.info("Successfully pushed %s", remote_image)
        return remote_image

    def analyze_image(self, image_name):
        """Summarise size, layer count and platform from docker inspect."""
        logger.info("Analyzing image: %s", image_name)
        info = json.loads(self._run(['inspect', image_name]) or '[]')
        if not info:
            return None
        image = info[0]
        size_mb = image.get('Size', 0) / (1024 * 1024)
        return {
            'size_mb': round(size_mb, 2),
            'layers': len((image.get('RootFS') or {}).get('Layers') or []),
            'os': image.get('Os', 'unknown'),
            'arch': image.get('Architecture', 'unknown'),
        }
