variable "subscription_id" {
  description = "Azure subscription id."
  type        = string
}

variable "location" {
  description = "Azure region."
  type        = string
  default     = "koreacentral"
}

variable "name_prefix" {
  description = "Resource name prefix."
  type        = string
  default     = "actocr"
}

variable "admin_username" {
  description = "VM admin username."
  type        = string
  default     = "azureuser"
}

variable "vm_size" {
  description = "VM size. Qwen2-VL CPU inference needs memory; Standard_D8s_v5 is a practical default."
  type        = string
  default     = "Standard_D8s_v5"
}

variable "os_disk_size_gb" {
  description = "OS disk size."
  type        = number
  default     = 128
}

variable "allowed_ssh_cidr" {
  description = "CIDR allowed to SSH. Use your public IP /32."
  type        = string
  default     = "0.0.0.0/0"
}

variable "allowed_app_cidr" {
  description = "CIDR allowed to access app ports."
  type        = string
  default     = "0.0.0.0/0"
}

variable "source_app_path" {
  description = "Local path to the actuarial OCR app directory containing docker-compose.yml. Leave empty to only create infrastructure."
  type        = string
  default     = ""
}

variable "deploy_app" {
  description = "Upload source_app_path to the VM and run docker compose."
  type        = bool
  default     = true
}

variable "start_qwen" {
  description = "Start Qwen container after deployment."
  type        = bool
  default     = false
}

variable "azure_ai_sku" {
  description = "Azure AI Vision SKU."
  type        = string
  default     = "S1"
}

