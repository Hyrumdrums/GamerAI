# 🎮 GamerAI Local Testing Guide

## Quick Start

```bash
# 1. Start the system (with local Ollama for real inference)
MOCK_INFERENCE=false docker compose --profile local-inference up

# 1b. Or skip Ollama and use mock workers
docker compose up

# 2. Test inference
python client/client.py "Explain quantum computing briefly"

# 3. Open web UI
open http://localhost:8080

# 4. View admin dashboard  
open http://localhost:8080/admin
```

## System Components

| Service | Port | Purpose | Status Check |
|---------|------|---------|--------------|
| **Coordinator** | 8000 | Job queue, worker registry | `curl localhost:8000/workers` |
| **Client Web UI** | 8080 | Submit prompts, view results | `curl localhost:8080` |
| **Redis** | 6379 | Job queue, worker state | `docker exec gamerai-redis redis-cli ping` |
| **Ollama** | 11435 | LLM inference | `curl localhost:11435/api/tags` |
| **Worker** | - | Processes inference jobs | See logs |

## Testing Scenarios

### 1. **Basic Inference Test**
```bash
# CLI client
python client/client.py "What is machine learning?"

# Expected: Real AI response + earnings calculation
# Output: job_id, worker_id, tokens, earnings, latency
```

### 2. **Web UI Test**
1. Go to http://localhost:8080
2. Enter prompt: "Explain neural networks"
3. Click submit
4. Watch real-time status updates
5. View result with worker details

### 3. **Multiple Workers Test** 
```bash
# Scale to 3 workers
docker compose up --scale worker=3

# Submit multiple jobs simultaneously
python client/client.py "Job 1" &
python client/client.py "Job 2" &
python client/client.py "Job 3" &
wait
```

### 4. **Admin Dashboard**
Visit http://localhost:8080/admin to see:
- 🟢 Active workers and their status
- 📊 Real-time job queue state  
- 💰 Earnings by worker
- 📈 System metrics and job history

### 5. **Earnings Verification**
```bash
# Check total earnings
python client/client.py --earnings

# Check specific worker earnings  
curl localhost:8000/earnings/worker-[ID]

# Formula verification:
# earnings = completion_tokens × $0.000005 × 0.7 (worker share)
```

### 6. **Windows Agent Simulation**
```bash
# Test Windows gamer agent (simulated on Linux)
cd windows-agent
python agent.py --once

# Should register, pick up job, complete it, show earnings
```

## Performance Testing

### Load Test
```bash
# Test concurrent job processing
for i in {1..10}; do
  python client/client.py "Test job $i" &
done
wait
```

### Model Switching
```bash
# Test different models (if available)
MODEL=llama3.2:3b docker compose up worker

# Or pull new model
docker exec gamerai-ollama ollama pull codellama:7b
```

## Monitoring & Debugging

### Live Logs
```bash
# All services
docker compose logs -f

# Specific service
docker compose logs -f worker
docker compose logs -f coordinator
```

### Database Inspection
```bash
# Check job history
sqlite3 data/gamerai.db "SELECT job_id, status, worker_id, earnings FROM jobs ORDER BY submitted_at DESC LIMIT 10;"

# Check worker earnings
sqlite3 data/gamerai.db "SELECT * FROM earnings ORDER BY total_usd DESC;"
```

### Redis State
```bash
# Check job queue
docker exec gamerai-redis redis-cli LLEN job_queue

# Check active workers  
docker exec gamerai-redis redis-cli SMEMBERS worker_registry

# Check worker heartbeats
docker exec gamerai-redis redis-cli HGETALL worker_heartbeats
```

## Expected User Experience

1. **Submit prompt** → System queues job
2. **Worker picks up** → Real inference with Ollama  
3. **Results returned** → Text + earnings calculated
4. **Persistence** → Job history + worker earnings stored
5. **Monitoring** → Admin dashboard shows system state

## Troubleshooting

### No Workers Available
```bash
# Check worker logs
docker compose logs worker

# Verify Ollama health
curl localhost:11435/api/tags
```

### Jobs Stuck in Queue
```bash
# Check Redis queue
docker exec gamerai-redis redis-cli LLEN job_queue

# Restart workers
docker compose restart worker
```

### Ollama Issues
```bash
# Check model availability
docker exec gamerai-ollama ollama list

# Test direct inference
docker exec gamerai-ollama ollama run llama3.2:1b "Hello"
```

## Business Model Validation

The system implements a **real marketplace**:
- **Workers earn**: $0.000005 per token × 70% = ~$0.0035 per 1000 tokens
- **Platform takes**: 30% = ~$0.0015 per 1000 tokens  
- **Real costs tracked**: Database stores all earnings
- **Scalable**: Can add workers across network

Perfect for validating the distributed inference economy!