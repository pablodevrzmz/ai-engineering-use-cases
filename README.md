# AI engineering use cases

Small, complete projects that back the architectures I write about on LinkedIn. Each folder is a working
reference you can deploy in your own provider, read in one sitting, and tear down
with one command. They are teaching material first: minimal code, real managed services.

| Use case | What it shows | Stack |
|---|---|---|
| [build-artifacts-aws-bedrock](build-artifacts-aws-bedrock/) | An agent that hands the user a file ("artifacts"): sandboxed code execution, a publish step into S3, a download card in the chat and a presigned download link | Amazon Bedrock AgentCore (Runtime + Code Interpreter), Amazon Nova, Strands Agents, ECS Fargate, ALB, Cognito, DynamoDB, S3 |
| [harness-vs-runtime-aws-bedrock](harness-vs-runtime-aws-bedrock/) | The same web-search agent deployed two ways: as an AgentCore harness (one Terraform resource, no code, no container) and as your own Strands code on AgentCore Runtime (container), sharing one model, one prompt and one gateway tool; a chat client and a smoke test compare them | Amazon Bedrock AgentCore (harness, Runtime, Gateway + Web Search Tool), Amazon Nova, Strands Agents, Terraform |

Every project has its own README with prerequisites, a `make` flow, a smoke test and a destroy step. Costs are
noted per project; destroy what you deploy when you are done.

Licensed under the [MIT License](LICENSE).
