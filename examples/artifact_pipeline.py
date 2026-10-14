"""Produce a dataset, train a model, and evaluate its immutable artifact.

Architecture: dataset run -> versioned dataset -> training run -> model artifact
-> evaluation run -> joined prediction table and charts. Handles are passed
between stages after upload; cross-client alias visibility can lag.
Main caller: CLI. Requires online mode because use_artifact downloads real files.
"""
import json
from pathlib import Path

import backprop as wandb
from common import parser, positive_int, prepare, record, start
from synthetic import make_data, measure, train_step, write_probability_image


def run_pipeline(args):
    """Keep each pipeline stage and its input/output relationships inspectable.

    Main caller: main. Dataset and model bytes are read back from downloaded
    artifacts, not substituted from producer memory, so this exercises storage.
    """
    runs = []
    rows = make_data(41, 128)
    dataset_file = args.output / 'dataset.json'
    dataset_file.write_text(json.dumps(rows))
    with start(args, 'Pipeline / dataset', job_type='dataset', config={'seed': 41}) as producer:
        runs.append(producer)
        dataset = wandb.Artifact(args.execution + '-dataset', 'dataset',
                                description='Synthetic binary classification samples',
                                metadata={'rows': len(rows), 'features': 2})
        dataset.add_file(dataset_file, name='samples.json')
        producer.log_artifact(dataset, aliases=['latest', 'training']).wait(timeout=120)
        producer.summary['rows'] = len(rows)
    with start(args, 'Pipeline / train', job_type='train',
               config={'epochs': args.steps, 'learning_rate': 0.2}) as trainer:
        runs.append(trainer)
        input_data = trainer.use_artifact(dataset)
        downloaded = Path(input_data.download(root=str(args.output / 'input-dataset')))
        train_rows = json.loads((downloaded / 'samples.json').read_text())
        weights = [0.0] * 3
        trainer.define_metric('epoch')
        trainer.define_metric('train/*', step_metric='epoch')
        for epoch in range(args.steps):
            weights = train_step(weights, train_rows, 0.2)
            loss, accuracy, _ = measure(weights, train_rows)
            trainer.log({'epoch': epoch, 'train/loss': loss, 'train/accuracy': accuracy})
        checkpoint = args.output / 'model.json'
        checkpoint.write_text(json.dumps({'weights': weights}))
        model = wandb.Artifact(args.execution + '-model', 'model', metadata={'epochs': args.steps})
        model.add_file(checkpoint, name='weights.json')
        trainer.log_artifact(model, aliases=['latest', 'candidate']).wait(timeout=120)
    with start(args, 'Pipeline / evaluate', job_type='evaluation', config={'seed': 71}) as evaluator:
        runs.append(evaluator)
        consumed = evaluator.use_artifact(model)
        downloaded = Path(consumed.download(root=str(args.output / 'input-model')))
        weights = json.loads((downloaded / 'weights.json').read_text())['weights']
        evaluation = make_data(71, 48)
        loss, accuracy, probabilities = measure(weights, evaluation)
        truth = wandb.Table(columns=['sample_id', 'label'], data=[
            [index, label] for index, (_, label) in enumerate(evaluation)])
        predictions = wandb.Table(columns=['sample_id', 'probability', 'prediction'], data=[
            [index, p, int(p >= .5)] for index, p in enumerate(probabilities)])
        image = args.output / 'probability.png'
        write_probability_image(image, weights)
        joined = wandb.JoinedTable(truth, predictions, 'sample_id')
        evaluation_artifact = wandb.Artifact(args.execution + '-evaluation', 'evaluation')
        evaluation_artifact.add(joined, 'predictions')
        evaluator.log_artifact(evaluation_artifact, aliases=['latest']).wait(timeout=120)
        evaluator.log({'eval/loss': loss, 'eval/accuracy': accuracy,
                       'evaluation/predictions': joined,
                       'evaluation/probability_map': wandb.Image(image),
                       'evaluation/confusion': wandb.plot.confusion_matrix(
                           y_true=[label for _, label in evaluation],
                           preds=[int(p >= .5) for p in probabilities], class_names=['negative', 'positive'])})
        evaluator.summary['accuracy'] = accuracy
        consumed.metadata['evaluation_accuracy'] = accuracy
        consumed.description = 'Trained model with held-out evaluation results'
        consumed.aliases = list(dict.fromkeys([*consumed.aliases, 'evaluated']))
        consumed.save()
    return record(args, runs, artifacts={'dataset': dataset.qualified_name, 'model': model.qualified_name},
                  evaluation={'loss': loss, 'accuracy': accuracy})


def main():
    cli = parser(__doc__, online_only=True)
    cli.add_argument('--steps', type=positive_int, default=40)
    run_pipeline(prepare(cli.parse_args(), 'pipeline'))


if __name__ == '__main__':
    main()
