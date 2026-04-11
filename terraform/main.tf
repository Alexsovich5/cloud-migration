# Cloud Migration Framework - Terraform Configuration
# IT Operations Specialist - ACORIA (2015)
# AWS infrastructure provisioning for migrated workloads

provider "aws" {
  region = var.aws_region
}

# VPC for migrated workloads
resource "aws_vpc" "migration" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = {
    Name    = "migration-vpc"
    Project = "cloud-migration"
  }
}

# Public subnet
resource "aws_subnet" "public" {
  count             = 2
  vpc_id            = aws_vpc.migration.id
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, count.index)
  availability_zone = "${var.aws_region}${element(["a", "b"], count.index)}"

  map_public_ip_on_launch = true

  tags = {
    Name = "migration-public-${count.index}"
  }
}

# Private subnet
resource "aws_subnet" "private" {
  count             = 2
  vpc_id            = aws_vpc.migration.id
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, count.index + 10)
  availability_zone = "${var.aws_region}${element(["a", "b"], count.index)}"

  tags = {
    Name = "migration-private-${count.index}"
  }
}

# Internet Gateway
resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.migration.id

  tags = {
    Name = "migration-igw"
  }
}

# NAT Gateway
resource "aws_nat_gateway" "main" {
  allocation_id = aws_eip.nat.id
  subnet_id     = aws_subnet.public[0].id

  tags = {
    Name = "migration-nat"
  }
}

resource "aws_eip" "nat" {
  vpc = true
}

# Security group for migrated applications
resource "aws_security_group" "app" {
  name_prefix = "migration-app-"
  vpc_id      = aws_vpc.migration.id

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
    cidr_blocks = [var.admin_cidr]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "migration-app-sg"
  }
}

# RDS subnet group
resource "aws_db_subnet_group" "migration" {
  name       = "migration-db-subnet"
  subnet_ids = aws_subnet.private[*].id

  tags = {
    Name = "migration-db-subnet-group"
  }
}

# S3 bucket for migration artifacts
resource "aws_s3_bucket" "artifacts" {
  bucket = "${var.project_name}-migration-artifacts"
  acl    = "private"

  versioning {
    enabled = true
  }

  server_side_encryption_configuration {
    rule {
      apply_server_side_encryption_by_default {
        sse_algorithm = "AES256"
      }
    }
  }

  tags = {
    Name    = "migration-artifacts"
    Project = "cloud-migration"
  }
}

output "vpc_id" {
  value = aws_vpc.migration.id
}

output "public_subnet_ids" {
  value = aws_subnet.public[*].id
}

output "private_subnet_ids" {
  value = aws_subnet.private[*].id
}

output "app_security_group_id" {
  value = aws_security_group.app.id
}
