output "public_ip" {
  value = aws_eip.mitra.public_ip
}

output "base_url" {
  value = "https://${local.domain}"
}

output "instance_id" {
  value = aws_instance.mitra.id
}

output "admin_token_command" {
  description = "Fetch the operator token generated on first boot (it never passes through Terraform state)."
  value       = "aws ssm get-parameter --region ${var.region} --with-decryption --name /mitra/${var.env}/admin_token --query Parameter.Value --output text"
}

output "shell_command" {
  value = "aws ssm start-session --region ${var.region} --target ${aws_instance.mitra.id}"
}
