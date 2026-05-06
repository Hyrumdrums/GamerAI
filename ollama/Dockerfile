FROM ollama/ollama:latest

# Set the data directory
ENV OLLAMA_HOST=0.0.0.0

# Create models directory
RUN mkdir -p /root/.ollama

# Expose Ollama port
EXPOSE 11434

# The ollama image already has the correct entrypoint