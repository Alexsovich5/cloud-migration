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
  default     = "acoria"
}
