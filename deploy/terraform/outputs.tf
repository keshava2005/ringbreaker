output "public_ip" {
  description = "Elastic IP. Put this in your is-a.dev A record."
  value       = aws_eip.host.public_ip
}

output "test_url" {
  description = "Works before DNS: public demo over plain HTTP by IP (the live app is refused here)"
  value       = "http://${aws_eip.host.public_ip}/"
}

output "site_url" {
  value = var.domain == "" ? "(set -var domain=... once the DNS record exists)" : "https://${var.domain}/"
}

output "live_url" {
  value = var.domain == "" ? "(needs the domain)" : "https://${var.domain}/live/"
}

output "instance_id" {
  value = aws_instance.host.id
}

output "shell" {
  description = "Admin shell without SSH (needs the Session Manager plugin for the AWS CLI)"
  value       = "aws ssm start-session --region ${var.region} --target ${aws_instance.host.id}"
}

output "redeploy" {
  description = "After `terraform apply` uploads a new bundle, run this to rebuild on the host"
  value       = "aws ssm send-command --region ${var.region} --instance-ids ${aws_instance.host.id} --document-name AWS-RunShellScript --parameters commands=/opt/ringbreaker/deploy/bootstrap.sh --comment ringbreaker-redeploy"
}

output "bootstrap_log" {
  value = "aws ssm start-session --region ${var.region} --target ${aws_instance.host.id}  then: sudo tail -f /var/log/ringbreaker-bootstrap.log"
}
