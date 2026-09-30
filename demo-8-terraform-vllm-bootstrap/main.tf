resource "aws_instance" "vllm_bench" {
  ami                    = data.aws_ami.ubuntu.id
  instance_type          = var.instance_type
  key_name               = var.key_name
  subnet_id              = data.aws_subnets.default.ids[0]
  vpc_security_group_ids = [aws_security_group.llm_bench.id]

  root_block_device {
    volume_size           = var.root_volume_size_gb
    volume_type            = "gp3"
    delete_on_termination  = true # ensures `terraform destroy` leaves no orphaned EBS volume
  }

  user_data = templatefile("${path.module}/templates/user_data.sh.tpl", {
    github_repo_url       = var.github_repo_url
    demo_dir_name          = var.demo_dir_name
    auto_shutdown_minutes  = var.auto_shutdown_minutes
  })

  # Forces re-provisioning if the bootstrap logic changes — otherwise
  # Terraform won't re-run user_data on an already-running instance.
  user_data_replace_on_change = true

  tags = {
    Name    = "vllm-qwen3-8b-benchmark"
    Project = "mlops-learning"
  }
}
