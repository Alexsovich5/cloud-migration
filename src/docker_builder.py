"""
Docker Builder Module
IT Operations Specialist - ACORIA (2015)

Handles containerization of workloads for cloud migration,
including Dockerfile generation, image building, and ECR push.
"""

import os
import json
import logging
import subprocess

logger = logging.getLogger('docker_builder')


class DockerBuilder:
    """Manages Docker containerization for migration workloads."""

    def __init__(self, config):
        self.registry = config.get('registry', '')
        self.base_images = config.get('base_images', {
            'python': 'python:3.4-slim',
            'java': 'openjdk:8-jre',
            'node': 'node:4-slim',
            'php': 'php:5.6-apache'
        })

    def generate_dockerfile(self, workload):
        """Generate a Dockerfile based on workload analysis."""
        runtime = workload.get('runtime', 'python')
        base = self.base_images.get(runtime, 'ubuntu:14.04')
        name = workload['name']

        dockerfile_lines = [
            f"FROM {base}",
            f"LABEL maintainer=\"migration-framework\"",
            f"LABEL project=\"{name}\"",
            "",
            "WORKDIR /app",
            ""
        ]

        # Add dependencies
        if runtime == 'python':
            dockerfile_lines.extend([
                "COPY requirements.txt .",
                "RUN pip install --no-cache-dir -r requirements.txt",
            ])
        elif runtime == 'node':
            dockerfile_lines.extend([
                "COPY package.json .",
                "RUN npm install --production",
            ])
        elif runtime == 'java':
            dockerfile_lines.extend([
                "COPY target/*.jar app.jar",
            ])

        dockerfile_lines.extend([
            "",
            "COPY . .",
            "",
            f"EXPOSE {workload.get('port', 8080)}",
            "",
        ])

        # Add entrypoint
        entrypoint = workload.get('entrypoint', '')
        if entrypoint:
            dockerfile_lines.append(f'CMD ["{entrypoint}"]')
        elif runtime == 'python':
            dockerfile_lines.append('CMD ["python", "app.py"]')
        elif runtime == 'java':
            dockerfile_lines.append('CMD ["java", "-jar", "app.jar"]')

        return '\n'.join(dockerfile_lines)

    def build_image(self, workload):
        """Build Docker image for a workload."""
        name = workload['name']
        tag = workload.get('version', 'latest')
        image_name = f"{name}:{tag}"

        logger.info("Building Docker image: %s", image_name)

        # Generate Dockerfile if needed
        dockerfile_path = os.path.join(workload.get('source_path', '.'), 'Dockerfile')
        if not os.path.exists(dockerfile_path):
            content = self.generate_dockerfile(workload)
            with open(dockerfile_path, 'w') as f:
                f.write(content)
            logger.info("Generated Dockerfile at %s", dockerfile_path)

        # Build image
        build_cmd = [
            'docker', 'build',
            '-t', image_name,
            '-f', dockerfile_path,
            workload.get('source_path', '.')
        ]

        try:
            result = subprocess.run(
                build_cmd,
                capture_output=True,
                text=True,
                timeout=600
            )
            if result.returncode != 0:
                logger.error("Docker build failed: %s", result.stderr)
                raise Exception(f"Build failed for {image_name}")
            logger.info("Successfully built %s", image_name)
            return image_name
        except subprocess.TimeoutExpired:
            logger.error("Docker build timed out for %s", image_name)
            raise

    def push_to_ecr(self, workload, aws_connector):
        """Push Docker image to AWS ECR."""
        name = workload['name']
        tag = workload.get('version', 'latest')
        ecr_uri = f"{self.registry}/{name}:{tag}"

        logger.info("Pushing image to ECR: %s", ecr_uri)

        # Tag for ECR
        local_image = f"{name}:{tag}"
        tag_cmd = ['docker', 'tag', local_image, ecr_uri]

        try:
            subprocess.run(tag_cmd, check=True, capture_output=True)
            push_cmd = ['docker', 'push', ecr_uri]
            subprocess.run(push_cmd, check=True, capture_output=True)
            logger.info("Successfully pushed %s", ecr_uri)
            return ecr_uri
        except subprocess.CalledProcessError as e:
            logger.error("Failed to push to ECR: %s", e.stderr)
            raise

    def analyze_image(self, image_name):
        """Analyze Docker image for security and size optimization."""
        logger.info("Analyzing image: %s", image_name)
        inspect_cmd = ['docker', 'inspect', image_name]

        try:
            result = subprocess.run(
                inspect_cmd, capture_output=True, text=True
            )
            info = json.loads(result.stdout)
            if info:
                size_mb = info[0].get('Size', 0) / (1024 * 1024)
                layers = len(info[0].get('RootFS', {}).get('Layers', []))
                return {
                    'size_mb': round(size_mb, 2),
                    'layers': layers,
                    'os': info[0].get('Os', 'unknown'),
                    'arch': info[0].get('Architecture', 'unknown')
                }
        except Exception as e:
            logger.warning("Image analysis failed: %s", e)
        return None
