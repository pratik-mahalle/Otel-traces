#!/bin/bash

# Multi-Agent Telemetry System Setup Script
# This script sets up the entire telemetry system in a Kind cluster

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Check prerequisites
check_prerequisites() {
    log_info "Checking prerequisites..."
    
    # Detect OS
    OS="$(uname -s)"
    ARCH="$(uname -m)"
    
    case "$OS" in
        Darwin)
            OS_TYPE="darwin"
            if [ "$ARCH" = "arm64" ]; then
                ARCH_TYPE="arm64"
            else
                ARCH_TYPE="amd64"
            fi
            ;;
        Linux)
            OS_TYPE="linux"
            ARCH_TYPE="amd64"
            ;;
        *)
            log_error "Unsupported OS: $OS"
            exit 1
            ;;
    esac
    
    log_info "Detected OS: $OS_TYPE, Arch: $ARCH_TYPE"
    
    # Check for Docker
    if ! command -v docker &> /dev/null; then
        log_error "Docker is not installed. Please install Docker first."
        exit 1
    fi
    
    # Check for Kind
    if ! command -v kind &> /dev/null; then
        log_warning "Kind is not installed. Installing..."
        curl -Lo ./kind "https://kind.sigs.k8s.io/dl/v0.20.0/kind-${OS_TYPE}-${ARCH_TYPE}"
        chmod +x ./kind
        if [ "$OS_TYPE" = "darwin" ]; then
            mkdir -p /usr/local/bin 2>/dev/null || true
            mv ./kind /usr/local/bin/kind 2>/dev/null || sudo mv ./kind /usr/local/bin/kind
        else
            sudo mv ./kind /usr/local/bin/kind
        fi
    fi
    
    # Check for kubectl
    if ! command -v kubectl &> /dev/null; then
        log_warning "kubectl is not installed. Installing..."
        curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/${OS_TYPE}/${ARCH_TYPE}/kubectl"
        chmod +x kubectl
        if [ "$OS_TYPE" = "darwin" ]; then
            mv kubectl /usr/local/bin/kubectl 2>/dev/null || sudo mv kubectl /usr/local/bin/kubectl
        else
            sudo mv kubectl /usr/local/bin/kubectl
        fi
    fi
    
    log_success "All prerequisites met!"
}

# Create Kind cluster
create_cluster() {
    log_info "Creating Kind cluster..."
    
    # Delete existing cluster if it exists
    if kind get clusters 2>/dev/null | grep -q "telemetry-cluster"; then
        log_warning "Existing cluster found. Deleting..."
        kind delete cluster --name telemetry-cluster
    fi
    
    # Create new cluster
    kind create cluster --config "${PROJECT_ROOT}/k8s/kind-config.yaml"
    
    # Wait for cluster to be ready
    log_info "Waiting for cluster to be ready..."
    kubectl wait --for=condition=Ready nodes --all --timeout=120s
    
    log_success "Kind cluster created successfully!"
}

# Build Docker images
build_images() {
    log_info "Building Docker images..."
    
    cd "$PROJECT_ROOT"
    
    # Build API image
    log_info "Building telemetry-api image..."
    docker build -f docker/Dockerfile.api -t telemetry-api:latest .
    
    # Build demo image
    log_info "Building telemetry-demo image..."
    docker build -f docker/Dockerfile.demo -t telemetry-demo:latest .
    
    # Load images into Kind cluster
    log_info "Loading images into Kind cluster..."
    kind load docker-image telemetry-api:latest --name telemetry-cluster
    kind load docker-image telemetry-demo:latest --name telemetry-cluster
    
    log_success "Docker images built and loaded!"
}

# Deploy infrastructure
deploy_infrastructure() {
    log_info "Deploying infrastructure..."
    
    # Create namespace
    kubectl apply -f - <<EOF
apiVersion: v1
kind: Namespace
metadata:
  name: telemetry
EOF
    
    # Deploy Kafka
    log_info "Deploying Kafka..."
    kubectl apply -f "${PROJECT_ROOT}/k8s/deployment.yaml" -l app=kafka
    
    # Wait for Kafka to be ready
    log_info "Waiting for Kafka to be ready..."
    kubectl -n telemetry wait --for=condition=Ready pod -l app=kafka --timeout=180s || true
    
    # Deploy Ollama
    log_info "Deploying Ollama..."
    kubectl apply -f "${PROJECT_ROOT}/k8s/deployment.yaml" -l app=ollama
    
    # Wait for Ollama to be ready
    log_info "Waiting for Ollama to be ready..."
    kubectl -n telemetry wait --for=condition=Ready pod -l app=ollama --timeout=180s || true
    
    log_success "Infrastructure deployed!"
}

# Create Kafka topics
create_kafka_topics() {
    log_info "Creating Kafka topics..."
    
    KAFKA_POD=$(kubectl -n telemetry get pods -l app=kafka -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "")
    
    if [ -n "$KAFKA_POD" ]; then
        TOPICS=("agent-telemetry-spans" "agent-telemetry-traces" "agent-telemetry-events" "agent-telemetry-handoffs" "agent-telemetry-metrics" "agent-telemetry-errors")
        
        for topic in "${TOPICS[@]}"; do
            kubectl -n telemetry exec "$KAFKA_POD" -- /opt/bitnami/kafka/bin/kafka-topics.sh \
                --create --if-not-exists \
                --topic "$topic" \
                --bootstrap-server localhost:9092 \
                --partitions 3 \
                --replication-factor 1 || true
        done
        
        log_success "Kafka topics created!"
    else
        log_warning "Kafka pod not ready yet. Topics will be auto-created."
    fi
}

# Pull Ollama model
pull_ollama_model() {
    log_info "Pulling Ollama model..."
    
    OLLAMA_POD=$(kubectl -n telemetry get pods -l app=ollama -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "")
    
    if [ -n "$OLLAMA_POD" ]; then
        kubectl -n telemetry exec "$OLLAMA_POD" -- ollama pull llama2 || true
        log_success "Ollama model pulled!"
    else
        log_warning "Ollama pod not ready yet. Model can be pulled later."
    fi
}

# Deploy applications
deploy_applications() {
    log_info "Deploying applications..."
    
    # Create ConfigMap for dashboard
    kubectl -n telemetry create configmap dashboard-config \
        --from-file="${PROJECT_ROOT}/src/dashboard/index.html" \
        --dry-run=client -o yaml | kubectl apply -f -
    
    # Deploy API and Dashboard
    kubectl apply -f "${PROJECT_ROOT}/k8s/deployment.yaml"
    
    # Wait for deployments
    log_info "Waiting for deployments to be ready..."
    kubectl -n telemetry wait --for=condition=Available deployment/telemetry-api --timeout=120s || true
    kubectl -n telemetry wait --for=condition=Available deployment/telemetry-dashboard --timeout=60s || true
    
    log_success "Applications deployed!"
}

# Print access information
print_access_info() {
    echo ""
    echo "=========================================="
    echo -e "${GREEN}Multi-Agent Telemetry System Deployed!${NC}"
    echo "=========================================="
    echo ""
    echo "Access URLs:"
    echo "  - Dashboard: http://localhost:8081"
    echo "  - API:       http://localhost:8080"
    echo "  - API Docs:  http://localhost:8080/docs"
    echo ""
    echo "Useful commands:"
    echo "  - View logs:  kubectl -n telemetry logs -f deployment/telemetry-api"
    echo "  - Run demo:   kubectl -n telemetry apply -f k8s/deployment.yaml -l app=multi-agent-demo"
    echo "  - Check pods: kubectl -n telemetry get pods"
    echo ""
    echo "To test with Claude Code:"
    echo "  1. Open the dashboard at http://localhost:8081"
    echo "  2. Run your multi-agent system with the telemetry SDK"
    echo "  3. Watch real-time events in the dashboard"
    echo ""
}

# Main execution
main() {
    echo ""
    echo "=========================================="
    echo "Multi-Agent Telemetry System Setup"
    echo "=========================================="
    echo ""
    
    check_prerequisites
    create_cluster
    build_images
    deploy_infrastructure
    
    # Wait a bit for pods to start
    sleep 10
    
    create_kafka_topics
    pull_ollama_model
    deploy_applications
    
    print_access_info
}

# Run main function
main "$@"
