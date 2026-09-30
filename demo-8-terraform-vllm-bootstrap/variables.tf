variable "aws_region" {
  description = "AWS region to launch the instance in."
  type        = string
  default     = "us-east-1"
}

variable "instance_type" {
  description = "GPU instance type. g6.xlarge (1x L4, 24GB VRAM) is the default used for this benchmark series."
  type        = string
  default     = "g6.xlarge"
}

variable "key_name" {
  description = "Name of an EXISTING EC2 key pair (must already exist in this region). No default — you must supply this."
  type        = string
}

variable "root_volume_size_gb" {
  description = "Root EBS volume size in GB. 100GB comfortably fits an 8B-class model plus the Python/CUDA toolchain."
  type        = number
  default     = 100
}

variable "github_repo_url" {
  description = "Repo to clone on first boot."
  type        = string
  default     = "https://github.com/engr-usman/aws-aiops-demo-repo.git"
}

variable "demo_dir_name" {
  description = "Subdirectory inside the cloned repo that holds the 01/02/03/04 scripts."
  type        = string
  default     = "demo-7-vllm-with-model-installation"
}

variable "auto_shutdown_minutes" {
  description = "Safety-net auto-shutdown timer (minutes) applied once bootstrap fully completes (server is up). Does NOT replace terraform destroy — it's a cost safety net only."
  type        = number
  default     = 180
}

variable "ubuntu_ami_name_filter" {
  description = "AMI name filter for the base OS. Defaults to Ubuntu 24.04 LTS (broadly available, well-supported). Override if you specifically want a different release."
  type        = string
  default     = "ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"
}
