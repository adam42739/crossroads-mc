# Container image for the Crossroads Discord-bot Lambda.
# Built, pushed to ECR, and wired up by CDK (DockerImageFunction) on `cdk deploy`.
FROM public.ecr.aws/lambda/python:3.12

# Install Python dependencies into the Lambda task root (on PYTHONPATH at runtime).
COPY pyproject.toml ./
RUN pip install --no-cache-dir uv \
    && uv pip install --no-cache --target "${LAMBDA_TASK_ROOT}" -r pyproject.toml

# Function code.
COPY src/discord_handler.py "${LAMBDA_TASK_ROOT}/"

# Handler: <module>.<function>
CMD ["discord_handler.handler"]
