locals {
  suffix       = random_string.suffix.result
  rg_name      = "${var.name_prefix}-${local.suffix}-rg"
  vm_name      = "${var.name_prefix}-${local.suffix}-vm"
  app_dir      = "/opt/actuarial-formula-page-ocr-local-regroup"
  compose_mode = var.start_qwen ? "qwen" : "base"
}

resource "random_string" "suffix" {
  length  = 6
  upper   = false
  special = false
}

resource "tls_private_key" "ssh" {
  algorithm = "RSA"
  rsa_bits  = 4096
}

resource "azurerm_resource_group" "this" {
  name     = local.rg_name
  location = var.location
}

resource "azurerm_virtual_network" "this" {
  name                = "${var.name_prefix}-${local.suffix}-vnet"
  location            = azurerm_resource_group.this.location
  resource_group_name = azurerm_resource_group.this.name
  address_space       = ["10.40.0.0/16"]
}

resource "azurerm_subnet" "app" {
  name                 = "app-subnet"
  resource_group_name  = azurerm_resource_group.this.name
  virtual_network_name = azurerm_virtual_network.this.name
  address_prefixes     = ["10.40.1.0/24"]
}

resource "azurerm_public_ip" "vm" {
  name                = "${var.name_prefix}-${local.suffix}-pip"
  location            = azurerm_resource_group.this.location
  resource_group_name = azurerm_resource_group.this.name
  allocation_method   = "Static"
  sku                 = "Standard"
}

resource "azurerm_network_security_group" "app" {
  name                = "${var.name_prefix}-${local.suffix}-nsg"
  location            = azurerm_resource_group.this.location
  resource_group_name = azurerm_resource_group.this.name

  security_rule {
    name                       = "Allow-SSH"
    priority                   = 100
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "22"
    source_address_prefix      = var.allowed_ssh_cidr
    destination_address_prefix = "*"
  }

  security_rule {
    name                       = "Allow-Frontend-8080"
    priority                   = 110
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "8080"
    source_address_prefix      = var.allowed_app_cidr
    destination_address_prefix = "*"
  }

  security_rule {
    name                       = "Allow-Backend-8000"
    priority                   = 120
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "8000"
    source_address_prefix      = var.allowed_app_cidr
    destination_address_prefix = "*"
  }
}

resource "azurerm_network_interface" "vm" {
  name                = "${var.name_prefix}-${local.suffix}-nic"
  location            = azurerm_resource_group.this.location
  resource_group_name = azurerm_resource_group.this.name

  ip_configuration {
    name                          = "ipconfig1"
    subnet_id                     = azurerm_subnet.app.id
    private_ip_address_allocation = "Dynamic"
    public_ip_address_id          = azurerm_public_ip.vm.id
  }
}

resource "azurerm_network_interface_security_group_association" "vm" {
  network_interface_id      = azurerm_network_interface.vm.id
  network_security_group_id = azurerm_network_security_group.app.id
}

resource "azurerm_cognitive_account" "vision" {
  name                = "${var.name_prefix}${local.suffix}vision"
  location            = azurerm_resource_group.this.location
  resource_group_name = azurerm_resource_group.this.name
  kind                = "ComputerVision"
  sku_name            = var.azure_ai_sku
}

resource "azurerm_linux_virtual_machine" "app" {
  name                = local.vm_name
  location            = azurerm_resource_group.this.location
  resource_group_name = azurerm_resource_group.this.name
  size                = var.vm_size
  admin_username      = var.admin_username
  network_interface_ids = [
    azurerm_network_interface.vm.id
  ]

  admin_ssh_key {
    username   = var.admin_username
    public_key = tls_private_key.ssh.public_key_openssh
  }

  os_disk {
    caching              = "ReadWrite"
    storage_account_type = "Premium_LRS"
    disk_size_gb         = var.os_disk_size_gb
  }

  source_image_reference {
    publisher = "Canonical"
    offer     = "0001-com-ubuntu-server-jammy"
    sku       = "22_04-lts-gen2"
    version   = "latest"
  }

  custom_data = base64encode(templatefile("${path.module}/cloud-init.yaml.tftpl", {
    admin_username    = var.admin_username
    app_dir           = local.app_dir
    azure_ai_endpoint = azurerm_cognitive_account.vision.endpoint
    azure_ai_key      = azurerm_cognitive_account.vision.primary_access_key
  }))
}

resource "null_resource" "deploy_app" {
  count = var.deploy_app && var.source_app_path != "" ? 1 : 0

  triggers = {
    vm_id           = azurerm_linux_virtual_machine.app.id
    source_app_path = var.source_app_path
    compose_mode    = local.compose_mode
  }

  connection {
    type        = "ssh"
    host        = azurerm_public_ip.vm.ip_address
    user        = var.admin_username
    private_key = tls_private_key.ssh.private_key_pem
    timeout     = "10m"
  }

  provisioner "remote-exec" {
    inline = [
      "sudo mkdir -p ${local.app_dir}",
      "sudo chown -R ${var.admin_username}:${var.admin_username} ${local.app_dir}",
      "rm -rf /tmp/app-upload",
      "mkdir -p /tmp/app-upload"
    ]
  }

  provisioner "file" {
    source      = var.source_app_path
    destination = "/tmp/app-upload"
  }

  provisioner "remote-exec" {
    inline = [
      "set -e",
      "sudo rm -rf ${local.app_dir}/*",
      "if [ -f /tmp/app-upload/docker-compose.yml ]; then cp -a /tmp/app-upload/. ${local.app_dir}/; else cp -a /tmp/app-upload/*/. ${local.app_dir}/; fi",
      "cd ${local.app_dir}",
      "cat > .env.azure-ai <<EOF\nAZURE_AI_ENDPOINT=${azurerm_cognitive_account.vision.endpoint}\nAZURE_AI_KEY=${azurerm_cognitive_account.vision.primary_access_key}\nAZURE_AI_API_VERSION=2024-02-01\nFORMULA_OCR_URL_FORMULA=http://formula-ocr:9000/predict\nFORMULA_OCR_URL_QWEN=http://qwen-vl:9100/predict\nFORMULA_OCR_URL_AZURE=http://azure-ai-ocr:9200/predict\nEOF",
      "cat > docker-compose.override.yml <<EOF\nservices:\n  backend:\n    environment:\n      FORMULA_OCR_URL_FORMULA: http://formula-ocr:9000/predict\n      FORMULA_OCR_URL_QWEN: http://qwen-vl:9100/predict\n      AZURE_AI_ENDPOINT: ${azurerm_cognitive_account.vision.endpoint}\n      AZURE_AI_KEY: ${azurerm_cognitive_account.vision.primary_access_key}\nEOF",
      "if grep -q 'FORMULA_OCR_URL:' docker-compose.yml && ! grep -q 'FORMULA_OCR_URL_AZURE' docker-compose.yml; then sed -i '/FORMULA_OCR_URL:/a\\      FORMULA_OCR_URL_AZURE: http://azure-ai-ocr:9200/predict\\n      AZURE_AI_ENDPOINT: ${azurerm_cognitive_account.vision.endpoint}\\n      AZURE_AI_KEY: ${azurerm_cognitive_account.vision.primary_access_key}' docker-compose.yml; fi",
      "docker compose --profile qwen build frontend backend formula-ocr qwen-vl",
      "if [ -f manual-start.sh ]; then chmod +x manual-start.sh; ./manual-start.sh ${local.compose_mode}; else docker compose --profile qwen up -d formula-ocr backend frontend; fi"
    ]
  }
}
