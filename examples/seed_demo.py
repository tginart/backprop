"""Run four tiny real optimizations and log a clearly labeled Foundry demo.

Architecture: seeded synthetic classification data -> four logistic-regression
fits -> SDK scalar/table/plot/media events -> native Foundry stream and files.
Every displayed measurement is calculated here; these are demonstration runs,
not production measurements. NumPy is required for automatic histograms.
Main callers: deployment smoke test and users trying the installed SDK.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
import tempfile

import backprop as wandb

from common import parser, positive_int, prepare, record, settings
from synthetic import make_data, measure, probability, write_probability_image


def seed(args, steps):
    """Train four learning rates and publish comparable metrics and rich outputs.

    Main caller: main. Uses the installed SDK and normal connection configuration.
    """
    train, evaluation = make_data(17, 256), make_data(23, 96)
    rates = [0.015, 0.045, 0.12, 0.3]
    names = ["Logistic / cautious", "Logistic / balanced", "Logistic / accelerated", "Logistic / aggressive"]
    results = []
    runs = []
    with tempfile.TemporaryDirectory(prefix="backprop-demo-") as directory:
        folder = Path(directory)
        for index, (learning_rate, name) in enumerate(zip(rates, names)):
            with wandb.init(project=args.project, entity=args.entity, name=name, mode=args.mode, dir=str(args.output),
                            group=args.execution, job_type="demo-training",
                            tags=["demo", "synthetic-data", "logistic-regression"],
                            config={"learning_rate": learning_rate, "epochs": steps,
                                    "model": "logistic regression", "dataset": "synthetic 2D classification",
                                    "train_examples": len(train), "eval_examples": len(evaluation),
                                    "seed": 17, "demonstration": True},
                            settings=settings()) as run:
                runs.append(run)
                run.define_metric("epoch")
                run.define_metric("train/*", step_metric="epoch")
                run.define_metric("eval/*", step_metric="epoch")
                weights = [0.0, 0.0, 0.0]
                epoch_loss = []
                samples = wandb.Table(columns=["epoch", "train_loss", "eval_loss", "accuracy"], log_mode="INCREMENTAL")
                for epoch in range(steps):
                    gradient = [0.0, 0.0, 0.0]
                    for features, label in train:
                        error = probability(weights, features) - label
                        for j in range(3):
                            gradient[j] += error * features[j] / len(train)
                    weights = [w - learning_rate * g for w, g in zip(weights, gradient)]
                    train_loss, train_acc, _ = measure(weights, train)
                    eval_loss, eval_acc, probabilities = measure(weights, evaluation)
                    epoch_loss.append(eval_loss)
                    metrics = {"epoch": epoch, "train/loss": train_loss, "train/accuracy": train_acc,
                               "train/gradient_norm": math.sqrt(sum(g*g for g in gradient)),
                               "train/learning_rate": learning_rate, "eval/loss": eval_loss, "eval/accuracy": eval_acc}
                    if epoch % 20 == 0:
                        samples.add_data(epoch, train_loss, eval_loss, eval_acc)
                        metrics["evaluation/checkpoints"] = samples
                    run.log(metrics, step=epoch, commit=True)
                prediction_table = wandb.Table(columns=["x", "y", "label", "probability", "prediction"], data=[
                    [features[0], features[1], label, p, int(p >= .5)]
                    for (features, label), p in zip(evaluation, probabilities)])
                weight_table = wandb.Table(columns=["parameter", "weight"], data=[
                    [label, value] for label, value in zip(["feature x", "feature y", "bias"], weights)])
                image = folder / f"probability-{index}.png"
                write_probability_image(image, weights)
                run.log({
                    "evaluation/predictions": prediction_table,
                    "evaluation/confidence": wandb.Histogram(probabilities, num_bins=20),
                    "evaluation/confusion_matrix": wandb.plot.confusion_matrix(
                        y_true=[label for _, label in evaluation], preds=[int(p >= .5) for p in probabilities],
                        class_names=["negative", "positive"], title="Evaluation confusion matrix"),
                    "model/weights": wandb.plot.bar(weight_table, "parameter", "weight", title="Learned weights"),
                    "evaluation/trajectory": wandb.plot.line_series(list(range(steps)), [epoch_loss],
                        keys=["eval loss"], title="Evaluation trajectory", xname="epoch"),
                    "evaluation/probability_map": wandb.Image(image, caption="Actual predicted probabilities on a 2D grid"),
                    "notes/demo": wandb.Html("<body style='background:#111820;color:#c7d5df;font:14px system-ui;padding:20px'>"
                        "<b>Backprop demonstration</b><p>Real logistic-regression optimization on seeded synthetic data. "
                        "All data in this demo is synthetic.</p></body>"),
                }, step=steps, commit=True)
                evidence = folder / f"model-{index}.json"
                evidence.write_text(json.dumps({"weights": weights, "learning_rate": learning_rate,
                                               "evaluation_loss": eval_loss, "evaluation_accuracy": eval_acc}, indent=2))
                artifact = wandb.Artifact(f"{args.execution}-weights-{index}", type="model-evidence",
                                         description="Demo model coefficients and measured evaluation results")
                artifact.add_file(evidence)
                run.log_artifact(artifact)
                run.summary.update({"best_eval_loss": min(epoch_loss), "final_accuracy": eval_acc,
                                    "demonstration": True})
            results.append({"id": run.id, "name": name, "eval_loss": round(eval_loss, 6), "accuracy": eval_acc})
            print(json.dumps(results[-1]), flush=True)
    record(args, runs, measurements=results)
    return results


def main():
    cli = parser(__doc__)
    cli.add_argument('--steps', type=positive_int, default=60)
    args = prepare(cli.parse_args(), 'comparison')
    seed(args, args.steps)


if __name__ == '__main__':
    main()
