#!/bin/bash
# Continuous load generator for the demo
echo "Starting continuous load generation..."
echo "Press Ctrl+C to stop"

while true; do
  echo "Running demo iteration..."
  kubectl -n telemetry delete job multi-agent-demo 2>/dev/null
  kubectl -n telemetry create job multi-agent-demo --image=telemetry-demo:latest -- python demo.py
  
  # Wait for job to complete (or just wait a fixed time)
  echo "Waiting for job to start..."
  kubectl -n telemetry wait --for=condition=ready pod -l job-name=multi-agent-demo --timeout=60s
  
  echo "Job running. Analyzing traces..."
  sleep 20 # Let it run for a bit
  
  echo "Cleaning up..."
done
