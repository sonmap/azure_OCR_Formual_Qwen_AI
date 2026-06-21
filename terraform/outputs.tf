output "resource_group_name" {
  value = azurerm_resource_group.this.name
}

output "public_ip" {
  value = azurerm_public_ip.vm.ip_address
}

output "app_url" {
  value = "http://${azurerm_public_ip.vm.ip_address}:8080/"
}

output "backend_url" {
  value = "http://${azurerm_public_ip.vm.ip_address}:8000/"
}

output "ssh_command" {
  value = "ssh -i ./generated-ssh.pem ${var.admin_username}@${azurerm_public_ip.vm.ip_address}"
}

output "private_key_pem" {
  value     = tls_private_key.ssh.private_key_pem
  sensitive = true
}

output "azure_ai_endpoint" {
  value = azurerm_cognitive_account.vision.endpoint
}

