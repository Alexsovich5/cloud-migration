"""
AWS Connector Module

Handles all AWS API interactions for infrastructure provisioning,
data migration, and health monitoring.
"""

import logging
import os
import uuid

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

import sizing

logger = logging.getLogger('aws_connector')

VOLUME_DEVICE = '/dev/xvdf'
DEFAULT_DB_PORTS = {'mysql': 3306, 'postgres': 5432}


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
        self.subnet_ids = aws_cfg.get('subnet_ids') or []
        self.shared_group_id = aws_cfg.get('security_group_id') or ''
        self.allowed_cidr = aws_cfg.get('allowed_cidr', '10.0.0.0/8')
        self.default_ami = aws_cfg.get('default_ami', '')

    def _client(self, svc, **kwargs):
        return self.session.client(svc, region_name=self.region,
                                   endpoint_url=self.endpoints.get(svc), **kwargs)

    @property
    def s3(self):
        """S3 client (signature v4, honours the configured endpoint)."""
        return self._s3

    def _record(self, state, workload_name, kind, **ids):
        state.record(workload_name, kind, **ids)
        state.save()

    def provision(self, workload, state):
        """Create security group, instance and optional volume for a workload.

        Every created resource is recorded in the state ledger (and the state
        file saved) as soon as it exists, so a failure part-way through leaves
        a ledger that rollback can work from.
        """
        name = workload['name']
        logger.info("Provisioning infrastructure for %s", name)

        sg_id = self._create_security_group(workload, state)
        instance_id = self._launch_instance(workload, sg_id, state)

        volume_id = None
        if workload.get('storage', 0) > 0:
            volume_id = self._create_volume(workload, instance_id, state)

        logger.info("Infrastructure provisioned for %s: instance=%s", name, instance_id)
        return {
            'security_group_id': sg_id,
            'instance_id': instance_id,
            'volume_id': volume_id,
        }

    def _create_security_group(self, workload, state):
        name = workload['name']
        # EC2 rejects a duplicate group name in the same VPC, so every
        # provisioning attempt gets its own suffix; the Name tag stays stable.
        group_name = 'migration-{0}-{1}'.format(name, uuid.uuid4().hex[:8])
        kwargs = {'GroupName': group_name,
                  'Description': 'Migrated workload {0}'.format(name)}
        if self.vpc_id:
            kwargs['VpcId'] = self.vpc_id
        sg_id = self.ec2.create_security_group(**kwargs)['GroupId']
        self.ec2.create_tags(Resources=[sg_id],
                             Tags=[{'Key': 'Name', 'Value': 'migration-' + name}])
        self._record(state, name, 'security_group', id=sg_id, name=group_name)

        for port in workload.get('ports') or []:
            self.ec2.authorize_security_group_ingress(
                GroupId=sg_id,
                IpPermissions=[{
                    'IpProtocol': 'tcp',
                    'FromPort': port,
                    'ToPort': port,
                    'IpRanges': [{'CidrIp': self.allowed_cidr}],
                }])
        return sg_id

    def _launch_instance(self, workload, sg_id, state):
        name = workload['name']
        kwargs = {
            'ImageId': workload.get('ami') or self.default_ami,
            'InstanceType': self._get_instance_type(workload),
            'MinCount': 1,
            'MaxCount': 1,
            'SecurityGroupIds': [sg_id],
        }
        if self.shared_group_id:
            kwargs['SecurityGroupIds'].append(self.shared_group_id)
        if self.subnet_ids:
            kwargs['SubnetId'] = self.subnet_ids[0]
        instance_id = self.ec2.run_instances(**kwargs)['Instances'][0]['InstanceId']
        self.ec2.create_tags(
            Resources=[instance_id],
            Tags=[
                {'Key': 'Name', 'Value': name},
                {'Key': 'Project', 'Value': 'cloud-migration'},
                {'Key': 'ManagedBy', 'Value': 'migration-framework'},
            ])
        self._record(state, name, 'instance', id=instance_id)
        return instance_id

    def _get_instance_type(self, workload):
        """Determine instance type from workload profile."""
        return sizing.recommend_instance(workload, self.catalog)

    def _describe_instance(self, instance_id):
        response = self.ec2.describe_instances(InstanceIds=[instance_id])
        for reservation in response.get('Reservations', []):
            for instance in reservation.get('Instances', []):
                if instance.get('InstanceId') == instance_id:
                    return instance
        return None

    def _volume_zone(self, instance_id):
        instance = self._describe_instance(instance_id) or {}
        zone = (instance.get('Placement') or {}).get('AvailabilityZone')
        if zone and zone != 'None':
            return zone
        # moto reports no placement zone; use the subnet's zone or <region>a.
        if self.subnet_ids:
            subnets = self.ec2.describe_subnets(SubnetIds=[self.subnet_ids[0]])
            for subnet in subnets.get('Subnets', []):
                if subnet.get('AvailabilityZone'):
                    return subnet['AvailabilityZone']
        return self.region + 'a'

    def _create_volume(self, workload, instance_id, state):
        zone = self._volume_zone(instance_id)
        volume_id = self.ec2.create_volume(
            Size=workload['storage'], VolumeType='gp2', AvailabilityZone=zone)['VolumeId']
        self.ec2.attach_volume(VolumeId=volume_id, InstanceId=instance_id,
                               Device=VOLUME_DEVICE)
        self._record(state, workload['name'], 'volume', id=volume_id,
                     instance_id=instance_id, device=VOLUME_DEVICE)
        return volume_id

    def create_database(self, workload, state):
        """Create an encrypted RDS instance for the workload's database block."""
        db = workload['database']
        env_name = db.get('password_env')
        if not env_name:
            raise ValueError('{0}.database.password_env: required'.format(workload['name']))
        password = os.environ.get(env_name)
        if not password:
            raise ValueError('{0}.database.password_env: environment variable {1} '
                             'is not set'.format(workload['name'], env_name))
        identifier = 'migrated-{0}'.format(db['name'].replace('_', '-'))
        logger.info("Creating RDS instance %s (engine=%s)", identifier, db.get('engine'))
        engine = db.get('engine', 'mysql')
        # Port is always sent: moto renders an unset port as <Port>None</Port>,
        # which botocore cannot parse as an integer.
        response = self.rds.create_db_instance(
            DBInstanceIdentifier=identifier,
            DBInstanceClass=sizing.recommend_db_class(db, self.catalog),
            Engine=engine,
            Port=db.get('port') or DEFAULT_DB_PORTS.get(engine, 3306),
            MasterUsername=db.get('username', 'admin'),
            MasterUserPassword=password,
            AllocatedStorage=db.get('storage', sizing.DEFAULT_DB_STORAGE),
            StorageEncrypted=True,
            MultiAZ=db.get('multi_az', False))
        db_id = response['DBInstance']['DBInstanceIdentifier']
        self._record(state, workload['name'], 'db_instance', id=db_id)
        return db_id

    def instance_state(self, instance_id):
        """Return the EC2 state name of an instance, or None if it is unknown."""
        instance = self._describe_instance(instance_id)
        if instance is None:
            return None
        return (instance.get('State') or {}).get('Name')

    def _ignore_not_found(self, code, func, **kwargs):
        try:
            func(**kwargs)
        except ClientError as e:
            if e.response.get('Error', {}).get('Code') == code:
                return False
            raise
        return True

    def terminate_instance(self, instance_id):
        """Terminate an instance; False if it no longer exists."""
        return self._ignore_not_found('InvalidInstanceID.NotFound',
                                      self.ec2.terminate_instances,
                                      InstanceIds=[instance_id])

    def delete_volume(self, volume_id, instance_id, device):
        """Detach then delete a volume; False if it no longer exists."""
        if not self._ignore_not_found('InvalidVolume.NotFound', self.ec2.detach_volume,
                                      VolumeId=volume_id, InstanceId=instance_id,
                                      Device=device):
            return False
        return self._ignore_not_found('InvalidVolume.NotFound', self.ec2.delete_volume,
                                      VolumeId=volume_id)

    def delete_security_group(self, group_id):
        """Delete a security group; False if it no longer exists."""
        return self._ignore_not_found('InvalidGroup.NotFound',
                                      self.ec2.delete_security_group, GroupId=group_id)

    def delete_db_instance(self, identifier):
        """Delete an RDS instance without a final snapshot; False if absent.

        Existence is checked by listing all instances because moto's RDS
        errors carry no code botocore can read.
        """
        instances = self.rds.describe_db_instances().get('DBInstances', [])
        if not any(d.get('DBInstanceIdentifier') == identifier for d in instances):
            return False
        self.rds.delete_db_instance(DBInstanceIdentifier=identifier, SkipFinalSnapshot=True)
        return True
