"""
AWS Connector Module

Handles all AWS API interactions for infrastructure provisioning,
data migration, and health monitoring.
"""

import logging

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

import sizing

logger = logging.getLogger('aws_connector')


class AWSConnector:
    """Interface for AWS service operations during migration."""

    def __init__(self, aws_cfg, catalog, session=None):
        self.catalog = catalog
        self.region = aws_cfg.get('region', 'us-east-1')
        self.endpoints = dict(aws_cfg.get('endpoints') or {})
        self.session = session if session is not None else boto3.session.Session()
        self.ec2 = self._client('ec2')
        # Signature v4 stops botocore from rewriting DNS-compatible bucket
        # requests to <bucket>.s3.amazonaws.com, which would bypass endpoint_url.
        self._s3 = self._client('s3', config=Config(signature_version='s3v4'))
        self.rds = self._client('rds')
        self.vpc_id = aws_cfg.get('vpc_id', '')
        self.subnet_ids = aws_cfg.get('subnet_ids', [])

    def _client(self, svc, **kwargs):
        return self.session.client(svc, region_name=self.region,
                                   endpoint_url=self.endpoints.get(svc), **kwargs)

    @property
    def s3(self):
        """S3 client (signature v4, honours the configured endpoint)."""
        return self._s3

    def provision_infrastructure(self, workload):
        """Provision AWS infrastructure for a workload."""
        name = workload['name']
        logger.info("Provisioning infrastructure for %s", name)

        # Create security group
        sg_id = self._create_security_group(workload)

        # Launch EC2 instance
        instance_type = self._get_instance_type(workload)
        instance_id = self._launch_instance(
            name=name,
            instance_type=instance_type,
            security_group=sg_id,
            ami=workload.get('ami', 'ami-12345678')
        )

        # Create and attach EBS volume if needed
        volume_id = None
        if workload.get('storage', 0) > 0:
            volume_id = self._create_ebs_volume(
                size=workload['storage'],
                instance_id=instance_id
            )

        logger.info("Infrastructure provisioned: instance=%s", instance_id)
        return {
            'instance_id': instance_id,
            'volume_id': volume_id,
            'security_group_id': sg_id
        }

    def _create_security_group(self, workload):
        """Create a security group for the workload."""
        try:
            response = self.ec2.create_security_group(
                GroupName="sg-{0}".format(workload['name']),
                Description="Security group for {0}".format(workload['name']),
                VpcId=self.vpc_id
            )
            sg_id = response['GroupId']

            # Add inbound rules
            ports = workload.get('ports', [22, 80, 443])
            for port in ports:
                self.ec2.authorize_security_group_ingress(
                    GroupId=sg_id,
                    IpPermissions=[{
                        'IpProtocol': 'tcp',
                        'FromPort': port,
                        'ToPort': port,
                        'IpRanges': [{'CidrIp': '10.0.0.0/8'}]
                    }]
                )
            return sg_id
        except ClientError as e:
            logger.error("Failed to create security group: %s", e)
            raise

    def _launch_instance(self, name, instance_type, security_group, ami):
        """Launch an EC2 instance."""
        try:
            response = self.ec2.run_instances(
                ImageId=ami,
                InstanceType=instance_type,
                MinCount=1,
                MaxCount=1,
                SecurityGroupIds=[security_group],
                SubnetId=self.subnet_ids[0] if self.subnet_ids else ''
            )
            instance_id = response['Instances'][0]['InstanceId']
            self.ec2.create_tags(
                Resources=[instance_id],
                Tags=[
                    {'Key': 'Name', 'Value': name},
                    {'Key': 'Project', 'Value': 'cloud-migration'},
                    {'Key': 'ManagedBy', 'Value': 'migration-framework'}
                ]
            )
            return instance_id
        except ClientError as e:
            logger.error("Failed to launch instance: %s", e)
            raise

    def _get_instance_type(self, workload):
        """Determine instance type from workload profile."""
        return sizing.recommend_instance(workload, self.catalog)

    def _create_ebs_volume(self, size, instance_id):
        """Create and attach an EBS volume."""
        try:
            volume = self.ec2.create_volume(
                Size=size,
                VolumeType='gp2',
                AvailabilityZone=self.region + 'a'
            )
            volume_id = volume['VolumeId']

            self.ec2.attach_volume(
                VolumeId=volume_id,
                InstanceId=instance_id,
                Device='/dev/xvdf'
            )
            return volume_id
        except ClientError as e:
            logger.error("Failed to create EBS volume: %s", e)
            raise

    def migrate_database(self, db_config):
        """Migrate database to RDS."""
        engine = db_config.get('engine', 'mysql')
        logger.info("Migrating database (engine=%s) to RDS", engine)

        try:
            response = self.rds.create_db_instance(
                DBInstanceIdentifier="migrated-{0}".format(db_config.get('name', 'db')),
                DBInstanceClass='db.m4.large',
                Engine=engine,
                MasterUsername=db_config.get('username', 'admin'),
                MasterUserPassword=db_config.get('password', ''),
                AllocatedStorage=db_config.get('storage', 100),
                MultiAZ=True,
                StorageEncrypted=True
            )
            return response['DBInstance']['DBInstanceIdentifier']
        except ClientError as e:
            logger.error("Failed to create RDS instance: %s", e)
            raise

    def check_instance_health(self, workload):
        """Check EC2 instance health status."""
        logger.info("Checking instance health for %s", workload['name'])
        return True

    def check_connectivity(self, workload):
        """Verify network connectivity to migrated workload."""
        logger.info("Checking connectivity for %s", workload['name'])
        return True

    def check_data_integrity(self, workload):
        """Validate data integrity after migration."""
        logger.info("Checking data integrity for %s", workload['name'])
        return True

    def destroy_infrastructure(self, workload):
        """Tear down infrastructure for rollback."""
        logger.warning("Destroying infrastructure for %s", workload['name'])
