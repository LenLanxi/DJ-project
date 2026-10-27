"""Offline comparison of real media receipts; does not call APIs or open devices."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'modules'/'ai'))
from RealtimeMetrics import summarize, rank

PROVIDERS = ['AlibabaRealtime', 'DoubaoRTC', 'TencentTRTC']
FILES = ['AlibabaMedia.json', 'DoubaoMedia.json', 'TencentMedia.json']


def compare(folder):
    folder = Path(folder)
    samples, diagnostics, sources, missing = [], [], [], []
    for name in FILES:
        path = folder/name
        if not path.is_file():
            missing.append(name)
            continue
        receipt = json.loads(path.read_text(encoding='utf-8-sig'))
        entries = receipt.get('samples', [])
        if not isinstance(entries, list):
            raise ValueError('invalid_sample_list_'+name)
        for sample in entries:
            if sample.get('phase') == 'development':
                continue
            if sample.get('provider') not in PROVIDERS:
                raise ValueError('unknown_provider_'+name)
            if sample.get('phase') == 'diagnostic':
                diagnostics.append(sample)
                continue
            samples.append(sample)
        sources.append({'file': name, 'samples': len(entries)})
    rows = summarize(samples)
    # Delivery and verbatim instruction compliance answer different questions.
    # Preserve every strict failure; do not discard a genuinely measured audio
    # response merely because the assistant paraphrased an acknowledgement.
    delivered = [dict(s, ok=s.get('delivery_verified') is True,
                      media_verified=s.get('delivery_verified') is True)
                 for s in samples if s['task']=='speech']
    delivery_rows = summarize(delivered)
    capabilities = []
    for provider in PROVIDERS:
        measured = [s for s in samples if s['provider']==provider]
        valid = [s for s in measured if s.get('ok') is True and s.get('media_verified') is True]
        vision = [s for s in valid if s['task']=='vision']
        colors = {s.get('expected_color', s.get('expected_answer',
                  s.get('semantic_evidence',{}).get('expected_color'))) for s in vision}
        capabilities.append(dict(provider=provider, attempts=len(measured),
            successful=len(valid), speech_verified=any(s['task']=='speech' for s in valid),
            speech_delivery_verified=any(s.get('delivery_verified') is True for s in measured if s['task']=='speech'),
            tool_result_roundtrip_verified=any(s['task']=='status' for s in valid),
            counterfactual_vision_verified=bool({'红','蓝'} <= colors),
            physical_devices_verified=False))
    return dict(schema_version=1, generated_at=datetime.now(timezone.utc).isoformat(),
        scope='prerecorded_synthetic_media_no_drone_or_headset',
        source_files=sources, missing_receipts=missing, samples=samples, summary=rows,
        capabilities=capabilities, delivery_summary=delivery_rows,
        diagnostic_samples=diagnostics, diagnostic_summary=summarize(diagnostics),
        media_first_audio=rank(delivery_rows, task='speech', mode='automatic',
                              metric='first_audio_ms', providers=PROVIDERS),
        speech_first_audio=rank(rows, task='speech', mode='automatic',
                               metric='first_audio_ms', providers=PROVIDERS),
        speech_completed=rank(rows, task='speech', mode='automatic',
                             metric='completed_ms', providers=PROVIDERS),
        interpretation=['first_audio_is_decoded_or_received_speech_not_soundcard_playout',
            'three_repeats_are_descriptive_not_statistical_or_field_acceptance',
            'connection_and_start_api_latency_are_excluded_from_turn_latency',
            'different_native_and_websocket_clients_include_different_codec_buffering',
            'Qwen_and_Doubao_confirm_20ms_RMS_Tencent_confirms_40ms_RMS_and_reports_first_window',
            'exact_wording_checks_cloud_subtitles_not_an_independent_transcription_of_received_audio',
            'no_route_has_completed_the_real_aircraft_scenario'])


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--folder', type=Path, default=ROOT/'ScenarioAcceptance'/'Evidence'/'RealtimeComparison')
    parser.add_argument('--output', type=Path)
    args=parser.parse_args()
    value=compare(args.folder)
    target=args.output or args.folder/'Summary.json'
    target.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({'summary_file':str(target.resolve()),'capabilities':value['capabilities'],
                      'media_first_audio':value['media_first_audio'],
                      'strict_wording_ranking':value['speech_first_audio'],
                      'delivery_summary':value['delivery_summary']}, ensure_ascii=True))
