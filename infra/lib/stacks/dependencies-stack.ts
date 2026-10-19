// SPDX-License-Identifier: MIT-0
import * as cdk from 'aws-cdk-lib';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

import { AppConfig, paramPath } from '../config';
import { applyStandardTags } from '../mixins/tagging';

/**
 * The one Python dependency layer every function in the platform uses.
 *
 * DESTROY. A layer is rebuilt from `pyproject.toml` and `make build-lambdas`; nothing here is a
 * source of truth.
 *
 * **Its own stack because it replaced two identical ones, and the duplication was a real defect.**
 * `ApiStack` and `AgentStack` each declared a `LayerVersion` over the *same* `build/layer` asset —
 * one dependency closure, published twice, with independent version streams. They had drifted to v4
 * and v2, so the two halves of the platform could run different dependency versions while both
 * stacks reported success. Their descriptions also claimed different contents ("...for the auth
 * domain", "...for the tool functions") when both held the full union, which is the kind of comment
 * that makes a reader confident about something untrue.
 *
 * A dependency layer changes on every dependency bump, so it stays isolated from retained data
 * storage rather than putting library churn in a data-bearing stack.
 *
 * **Versioning is what a layer already gives us, and it is why the SSM handoff is safe.** Every
 * deploy that changes the asset publishes a new immutable `LayerVersion` and writes its ARN to SSM.
 * A consumer resolves that ARN at deploy time and pins it, so a consumer that has *not* been
 * redeployed keeps running the exact version it was built against — the safe direction. Picking up
 * a new layer is a deliberate redeploy, never something that happens underneath a function.
 */
export interface DependenciesStackProps extends cdk.StackProps {
  readonly config: AppConfig;
  /** Directory holding the built dependency layer. Produced by `make build-lambdas`. */
  readonly layerAssetPath: string;
}

export class DependenciesStack extends cdk.Stack {
  public readonly layer: lambda.LayerVersion;

  constructor(scope: Construct, id: string, props: DependenciesStackProps) {
    super(scope, id, props);

    // Every stack, without exception — see mixins/tagging. Applied through an Aspect, so
    // position in the constructor does not matter and every later resource is covered.
    applyStandardTags(this, props.config);
    const { config } = props;

    this.layer = new lambda.LayerVersion(this, 'PythonDependencies', {
      code: lambda.Code.fromAsset(props.layerAssetPath),
      compatibleRuntimes: [new lambda.Runtime(config.pythonRuntime, lambda.RuntimeFamily.PYTHON)],
      compatibleArchitectures: [lambda.Architecture.ARM_64],
      // Describes what it *is* rather than listing what it contains. The two descriptions this
      // replaced both enumerated a subset and were both wrong; `pyproject.toml` is the only honest
      // statement of the closure, and a description cannot be kept in step with it.
      description: `Python dependency closure for adlytics-${config.envName}, built from pyproject.toml`,
      // The layer is an artefact, and an orphaned LayerVersion is not free: it counts against the
      // account's layer storage. Removing it with the stack is right because any function still
      // referencing a version pins that version's ARN, so a live function is unaffected by the
      // stack being torn down and rebuilt.
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    new ssm.StringParameter(this, 'ParamDependenciesLayerArn', {
      parameterName: paramPath(config.envName, 'dependenciesLayerArn'),
      // The version ARN, not the layer ARN. A version-less ARN cannot be attached to a function, and
      // the version is the whole point: it is what makes a consumer's dependency set reproducible.
      stringValue: this.layer.layerVersionArn,
    });

    new cdk.CfnOutput(this, 'OutDependenciesLayerArn', {
      value: this.layer.layerVersionArn,
      description: 'The Python layer every function attaches. Republished on every dependency bump.',
    });
  }
}
