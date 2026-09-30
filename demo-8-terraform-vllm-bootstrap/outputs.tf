output "instance_id" {
  value = aws_instance.vllm_bench.id
}

output "public_ip" {
  value = aws_instance.vllm_bench.public_ip
}

output "ssh_command" {
  value = "ssh -i /path/to/${var.key_name}.pem ubuntu@${aws_instance.vllm_bench.public_ip}"
}

output "check_bootstrap_progress" {
  value = "Once SSH'd in, run: tail -f /var/log/bootstrap.log"
}

output "check_server_ready" {
  value = "Once bootstrap finishes (03-serve-started marker), run: tail -f ~/vllm.log  (look for 'Application startup complete')"
}

output "run_benchmark_manually" {
  value = "cd ~/aws-aiops-demo-repo/${var.demo_dir_name} && ./04-run-benchmark.sh"
}
