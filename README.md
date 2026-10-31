# AI engineering use cases

Small, complete projects that back the architectures I write about on LinkedIn. Each folder is a working
reference you can deploy in your own provider, read in one sitting, and tear down
with one command. They are teaching material first: minimal code, real managed services.

| Use case | What it shows | Stack |
|---|---|---|
| [build-artifacts-aws-bedrock](build-artifacts-aws-bedrock/) | An agent that hands the user a file ("artifacts"): sandboxed code execution, a publish step into S3, a download card in the chat and a presigned download link | Amazon Bedrock AgentCore (Runtime + Code Interpreter), Amazon Nova, Strands Agents, ECS Fargate, ALB, Cognito, DynamoDB, S3 |

Every project has its own README with prerequisites, a `make` flow, a smoke test and a destroy step. Costs are
noted per project; destroy what you deploy when you are done.

Licensed under the [MIT License](LICENSE).
