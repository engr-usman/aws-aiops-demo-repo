resource "aws_security_group" "llm_bench" {
  name        = "vllm-benchmark-sg"
  description = "SSH access for vLLM benchmark instance. Port 8000 (vLLM API) is not exposed externally - access it via SSH."
  vpc_id      = data.aws_vpc.default.id

  ingress {
    description = "SSH from allowed IP only"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "All outbound (needed for apt, pip, HuggingFace downloads, git clone)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name    = "vllm-benchmark-sg"
    Project = "mlops-learning"
  }
}
