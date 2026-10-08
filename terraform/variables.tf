variable "aws_region" {
  description = "AWS region for migration target"
  default     = "us-east-1"
}

variable "vpc_cidr" {
  description = "CIDR block for the migration VPC"
  default     = "10.100.0.0/16"
}

variable "admin_cidr" {
  description = "CIDR block for SSH admin access"
  default     = "10.0.0.0/8"
}

variable "project_name" {
  description = "Project identifier"
  default     = "demo"
}

variable "public_subnet_cidrs" {
  description = "Comma-separated CIDR blocks for the public subnets"
  default     = "10.100.0.0/24,10.100.1.0/24"
}

variable "private_subnet_cidrs" {
  description = "Comma-separated CIDR blocks for the private subnets"
  default     = "10.100.10.0/24,10.100.11.0/24"
}

variable "availability_zones" {
  description = "Comma-separated availability zones, one per subnet pair"
  default     = "us-east-1a,us-east-1b"
}
