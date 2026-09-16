#!/bin/bash
# Usage: ./switch_scenario.sh [crashloop|imagepull|oom]

SCENARIO=$1

if [ -z "$SCENARIO" ]; then
  echo "Usage: ./switch_scenario.sh [crashloop|imagepull|oom]"
  exit 1
fi

echo "Removing any existing broken-app deployment..."
kubectl delete deployment broken-app -n demo-app --ignore-not-found=true

echo "Deploying scenario: $SCENARIO"
kubectl apply -f "scenario-${SCENARIO}.yaml"

echo "Done. Watch status with: kubectl get pods -n demo-app -w"