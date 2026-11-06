"""Offline gate for human-labelled translations, including injected errors."""

import argparse
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Literal

from pydantic import Field, TypeAdapter, model_validator

from .intake import MAX_PASSAGE, normalize
from .models import LocalModels, Response, Translation
from .rules import Masker, digest
from .validation import LANGUAGES, Validator

CRITICAL_TYPES = {'polarity', 'speaker', 'evidentiality', 'quantity'}


class Fixture(Response):
    id: str = Field(min_length=1)
    complaint: str = Field(min_length=1)
    format: Literal['pdf', 'docx', 'doc', 'txt']
    language: str
    source: str = Field(min_length=1, max_length=MAX_PASSAGE)
    error_type: str | None = None
    critical: bool = False
    a: str | None = None
    b: str | None = None

    @model_validator(mode='after')
    def check_fixture(self):
        if self.language not in LANGUAGES:
            raise ValueError('Fixture language must be officer-confirmed.')
        if (self.a is None) != (self.b is None):
            raise ValueError('Provide both labelled candidates or omit both for a live smoke sample.')
        if self.error_type is not None and (self.a is None or self.language == 'en'):
            raise ValueError('Injected errors require two candidates and a translated source.')
        if self.critical and not self.error_type:
            raise ValueError('Critical fixtures require an error type.')
        return self


class EvaluationModels(LocalModels):
    fixture = None

    def chat(self, model, role, instruction, data, schema, trace, models, budget=700):
        if schema is Translation and self.fixture.a is not None:
            english = self.fixture.a if role == 'Translation A' else self.fixture.b
            trace.append({'role': role, 'model': 'held-out candidate', 'response_hash': digest(english)})
            return Translation(id=data['id'], english=english)
        return super().chat(model, role, instruction, data, schema, trace, models, budget)


def evaluate(fixtures):
    models = EvaluationModels()
    validator = Validator(models)
    groups = defaultdict(Counter)
    catches, totals, live_times = Counter(), Counter(), Counter()
    critical_escapes = false_alarms = correct = runtime_errors = 0
    results = []
    try:
        for fixture in fixtures:
            models.fixture = fixture
            source = normalize(fixture.source)
            result = validator.run({'id': fixture.id, 'effective_source': source, 'start': 0, 'end': len(source),
                                    'page': None, 'document_hash': digest(fixture.complaint),
                                    'language_override': fixture.language},
                                   Masker([source]), [], 'en')
            passed = result['verdict'] == 'pass'
            runtime_errors += result['slot'] == 'service'
            bucket = groups[f'{fixture.language}/{fixture.format}']
            bucket[result['verdict']] += 1
            if fixture.error_type is not None:
                totals[fixture.error_type] += 1
                catches[fixture.error_type] += not passed
                critical_escapes += fixture.critical and passed
                bucket['critical_escapes'] += fixture.critical and passed
            elif fixture.a is not None:
                correct += 1
                false_alarms += not passed
                bucket['false_alarms'] += not passed
            if fixture.a is None:
                live_times[fixture.complaint] += result['machine_ms']
            results.append({'id': fixture.id, 'language': fixture.language, 'format': fixture.format,
                            'error_type': fixture.error_type, 'verdict': result['verdict'], 'reason': result['reason'],
                            'machine_ms': result['machine_ms']})
    finally:
        models.close()
    rates = {kind: catches[kind] / total for kind, total in totals.items()}
    false_alarm_rate = false_alarms / correct if correct else None
    times = sorted(live_times.values())
    p50 = times[math.ceil(len(times) * 0.5) - 1] if times else None
    p95 = times[math.ceil(len(times) * 0.95) - 1] if times else None
    gate = {
        'critical_escapes_zero': critical_escapes == 0,
        'critical_error_type_coverage': CRITICAL_TYPES.issubset({
            fixture.error_type for fixture in fixtures if fixture.critical
        }),
        'critical_type_catch_rates': all(rates.get(kind, 0) >= 0.95 for kind in CRITICAL_TYPES),
        'false_alarm_budget': false_alarm_rate is not None and false_alarm_rate <= 0.10,
        'live_validation_p95_under_60_seconds': p95 is not None and p95 < 60000,
        'no_runtime_failures': runtime_errors == 0,
    }
    return {'passed': all(gate.values()), 'gate': gate, 'critical_escapes': critical_escapes, 'catch_rates': rates,
            'false_alarm_rate': false_alarm_rate, 'live_validation_p50_ms': p50, 'live_validation_p95_ms': p95,
            'groups': groups, 'results': results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('held_out', type=Path)
    args = parser.parse_args()
    fixtures = TypeAdapter(list[Fixture]).validate_json(args.held_out.read_bytes())
    report = evaluate(fixtures)
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if report['passed'] else 1)


if __name__ == '__main__':
    main()
