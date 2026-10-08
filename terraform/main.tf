# Landing zone for migrated workloads: VPC, public and private subnets,
# an internet-facing route table, an application security group, an RDS
# subnet group and an artifacts bucket that only accepts encrypted uploads.

provider "aws" {
  region = "${var.aws_region}"
}

resource "aws_vpc" "migration" {
  cidr_block           = "${var.vpc_cidr}"
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags {
    Name    = "migration-vpc"
    Project = "cloud-migration"
  }
}

resource "aws_subnet" "public" {
  count                   = 2
  vpc_id                  = "${aws_vpc.migration.id}"
  cidr_block              = "${element(split(",", var.public_subnet_cidrs), count.index)}"
  availability_zone       = "${element(split(",", var.availability_zones), count.index)}"
  map_public_ip_on_launch = true

  tags {
    Name = "migration-public-${count.index}"
  }
}

resource "aws_subnet" "private" {
  count             = 2
  vpc_id            = "${aws_vpc.migration.id}"
  cidr_block        = "${element(split(",", var.private_subnet_cidrs), count.index)}"
  availability_zone = "${element(split(",", var.availability_zones), count.index)}"

  tags {
    Name = "migration-private-${count.index}"
  }
}

resource "aws_internet_gateway" "main" {
  vpc_id = "${aws_vpc.migration.id}"

  tags {
    Name = "migration-igw"
  }
}

resource "aws_route_table" "public" {
  vpc_id = "${aws_vpc.migration.id}"

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = "${aws_internet_gateway.main.id}"
  }

  tags {
    Name = "migration-public"
  }
}

resource "aws_route_table_association" "public" {
  count          = 2
  subnet_id      = "${element(aws_subnet.public.*.id, count.index)}"
  route_table_id = "${aws_route_table.public.id}"
}

resource "aws_security_group" "app" {
  name        = "migration-app"
  description = "Inbound web and SSH for migrated applications"
  vpc_id      = "${aws_vpc.migration.id}"

  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["10.0.0.0/8"]
  }

  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["10.0.0.0/8"]
  }

  ingress {
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["${var.admin_cidr}"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags {
    Name = "migration-app-sg"
  }
}

resource "aws_db_subnet_group" "migration" {
  name        = "migration-db-subnet"
  description = "Private subnets for migrated RDS instances"
  subnet_ids  = ["${aws_subnet.private.*.id}"]
}

resource "aws_s3_bucket" "artifacts" {
  bucket = "${var.project_name}-migration-artifacts"
  acl    = "private"

  policy = <<POLICY
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "DenyUnencryptedUploads",
      "Effect": "Deny",
      "Principal": "*",
      "Action": "s3:PutObject",
      "Resource": "arn:aws:s3:::${var.project_name}-migration-artifacts/*",
      "Condition": {
        "StringNotEquals": {
          "s3:x-amz-server-side-encryption": "AES256"
        }
      }
    }
  ]
}
POLICY

  tags {
    Name    = "migration-artifacts"
    Project = "cloud-migration"
  }
}
