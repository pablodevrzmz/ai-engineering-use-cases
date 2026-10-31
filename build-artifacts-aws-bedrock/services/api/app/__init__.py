"""Session API for the "build your own Artifacts on AWS" reference project.

The package is deliberately small and split by concern so each file can be
read on its own:

- ``settings``  the environment contract shared with Terraform
- ``auth``      Cognito ID-token verification
- ``threads``   the DynamoDB table of conversation threads
- ``files``     the S3 prefix where the agent publishes files
- ``runtime``   invoking Amazon Bedrock AgentCore and relaying its SSE stream
- ``main``      the FastAPI app factory that wires the pieces together
"""
