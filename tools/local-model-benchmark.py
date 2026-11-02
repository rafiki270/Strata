"""Measure real, uncached prompt ingestion separately from output generation."""
import argparse
import hashlib
import json
import random
import time
from pathlib import Path

import requests


def build_messages(seed, length):
    rng = random.Random(seed)
    words = ['archive', 'invoice', 'release', 'review', 'project', 'filter', 'sample',
             'workshop', 'inventory', 'delivery', 'meeting', 'inspection']
    lines = [f'{seed} benchmark archive. Read the following records as data.\n']
    for i in range(max(5000, length // 45)):
        lines.append(f'Record {i:06d}: {rng.choice(words)} {rng.randrange(100000,999999)}; '
                     f'The team checked the {rng.choice(words)} and recorded the result for review.\n')
    text = ''.join(lines)[:length]
    return [{'role': 'user', 'content': text + '\nWrite a detailed numbered checklist of 100 practical '
             'steps for maintaining a software release archive. Give a full explanatory sentence for '
             'each step. Continue until all 100 steps are complete.'}]


def run(args):
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    def count(messages):
        if args.kind == 'avalon':
            r = session.post(args.url+'/apply-template', json={'messages':messages,
                'chat_template_kwargs':{'enable_thinking':False}}, timeout=120)
            r.raise_for_status()
            prompt = r.json()['prompt']
            r = session.post(args.url+'/tokenize', json={'content':prompt,'add_special':True}, timeout=120)
            r.raise_for_status()
            return len(r.json()['tokens'])
        r = session.post(args.url+'/v1/messages/count_tokens', json={'model':'strata','messages':messages}, timeout=120)
        r.raise_for_status()
        return r.json()['input_tokens']
    results = []
    for trial in range(args.runs):
        seed = f'run-{args.label}-{trial}-{time.time_ns()}'
        # Calibrate character length against the actual tokenizer, then find the target boundary.
        lo, hi = 0, args.prompt*10
        while lo+1 < hi:
            mid = (lo+hi)//2
            if count(build_messages(seed,mid)) <= args.prompt:
                lo = mid
            else:
                hi = mid
        messages = build_messages(seed,lo)
        token_count = count(messages)
        digest = hashlib.sha256(messages[0]['content'].encode()).hexdigest()
        body = {'model':'strata', 'messages':messages, 'max_tokens':args.output,
                'stream':True, 'stream_options':{'include_usage':True},
                'temperature':0, 'seed':42, 'reasoning_effort':'none',
                'chat_template_kwargs':{'enable_thinking':False}, 'cache_prompt':False}
        started = time.perf_counter()
        first = None
        chunks = []
        result = {'label':args.label, 'trial':trial+1, 'input_tokens_counted':token_count,
                  'requested_output_tokens':args.output, 'prompt_sha256':digest,
                  'url':args.url,'kind':args.kind, 'utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())}
        print(json.dumps({'starting':result}), flush=True)
        try:
            with session.post(args.url+'/v1/chat/completions', json=body, stream=True,
                              timeout=(15,args.timeout)) as r:
                r.raise_for_status()
                for raw in r.iter_lines(chunk_size=1):
                    if not raw.startswith(b'data:'):
                        continue
                    data = raw[5:].strip()
                    if data == b'[DONE]':
                        break
                    event = json.loads(data)
                    if event.get('error'):
                        raise RuntimeError(event['error'])
                    if event.get('usage'):
                        result['usage'] = event['usage']
                    if event.get('timings'):
                        result['timings'] = event['timings']
                    for choice in event.get('choices',[]):
                        delta = choice.get('delta',{})
                        text = delta.get('content') or delta.get('reasoning_content') or ''
                        if text and first is None:
                            first = time.perf_counter()-started
                        chunks.append(text)
                        if choice.get('finish_reason'):
                            result['finish_reason'] = choice['finish_reason']
            result['wall_seconds'] = time.perf_counter()-started
            result['time_to_first_token_seconds'] = first
            result['answer'] = ''.join(chunks)
            if not result.get('timings'):
                status = session.get(args.url+'/v1/status',timeout=10).json()
                result['timings'] = status.get('last_timings')
            timings = result.get('timings') or {}
            if timings.get('cache_n',0) > 16:
                result['invalid_reason'] = 'Unexpected prompt cache reuse'
            if not result['answer']:
                result['invalid_reason'] = 'No generated answer'
        except Exception as exc:
            result['error'] = f'{type(exc).__name__}: {exc}'
            result['wall_seconds'] = time.perf_counter()-started
        results.append(result)
        (out/f'{args.label}-{args.prompt}-{trial+1}.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
        print(json.dumps({k:v for k,v in result.items() if k!='answer'}),flush=True)
        if result.get('error'):
            break
    return results


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--url',required=True)
    parser.add_argument('--label',required=True)
    parser.add_argument('--kind',choices=['strata','avalon'],default='strata')
    parser.add_argument('--prompt',type=int,default=32768)
    parser.add_argument('--output',type=int,default=512)
    parser.add_argument('--runs',type=int,default=3)
    parser.add_argument('--timeout',type=int,default=3600)
    parser.add_argument('--out',default=str(Path(__file__).parent/'results'))
    run(parser.parse_args())
