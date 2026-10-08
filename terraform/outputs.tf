output "vpc_id" {
  value = "${aws_vpc.migration.id}"
}

output "public_subnet_ids" {
  value = "${join(",", aws_subnet.public.*.id)}"
}

output "private_subnet_ids" {
  value = "${join(",", aws_subnet.private.*.id)}"
}

output "app_security_group_id" {
  value = "${aws_security_group.app.id}"
}

output "db_subnet_group_name" {
  value = "${aws_db_subnet_group.migration.name}"
}

output "artifacts_bucket" {
  value = "${aws_s3_bucket.artifacts.id}"
}
