#!/bin/sh
# Run an offline terraform plan so the provider schema validation runs.
# The credential/refresh failure that follows validation is expected;
# only configuration errors fail this check.
terraform plan -input=false -refresh=false 2>&1 | tee /tmp/plan.out

if grep -q "There are warnings and/or errors related to your configuration" /tmp/plan.out; then
    echo "tf-plan: configuration errors reported" >&2
    exit 1
fi
if grep -q "Required variable not set" /tmp/plan.out; then
    echo "tf-plan: required variable not set" >&2
    exit 1
fi
echo "tf-plan: configuration passed schema validation"
exit 0
