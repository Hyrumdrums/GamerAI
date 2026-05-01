# infra/

Placeholder for future infrastructure-as-code (Terraform / CDK / Pulumi).

Planned resources for Phase 2 (AWS):

- **VPC** — public subnets for the coordinator ALB, private subnets for ElastiCache.
- **ElastiCache (Redis)** — replaces local `redis` container.
- **RDS or DynamoDB** — replaces SQLite as system of record.
- **ECS Fargate / App Runner** — runs the coordinator image.
- **EC2 (GPU) Auto Scaling Group** — runs worker images. Workers can also be
  externally hosted (gamer machines) connecting to the coordinator over public TLS.
- **API Gateway / ALB** — public endpoint for `/generate`.
- **CloudWatch / OTLP collector** — ingests structured JSON logs from all services.

Nothing here yet.
