"""Offline comparison of real media receipts; does not call APIs or open devices."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT/'ScenarioAcceptance'/'Evidence'/'RealtimeComparison'
DEFAULT = ROOT/'ScenarioAcceptance'/'Evidence'/'Current'/'RealtimeComparison'
sys.path.insert(0, str(ROOT))
from RealtimeMetrics import summarize, rank

PROVIDERS = ['AlibabaRealtime', 'DoubaoRTC']
FILES = ['AlibabaMedia.json', 'DoubaoMedia.json']
# Optional historical receipts remain readable after their live driver retired.
ARCHIVED_FILES = {'TencentMedia.json': 'TencentTRTC'}


def compare(folder):
    folder = Path(folder)
    archived = {name: provider for name, provider in ARCHIVED_FILES.items()
                if (folder/name).is_file()}
    providers = PROVIDERS + list(archived.values())
    samples, diagnostics, sources, missing = [], [], [], []
    for name in [*FILES, *archived]:
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
            if sample.get('provider') not in providers:
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
    for provider in providers:
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
        archived_providers=list(archived.values()),
        capabilities=capabilities, delivery_summary=delivery_rows,
        diagnostic_samples=diagnostics, diagnostic_summary=summarize(diagnostics),
        media_first_audio=rank(delivery_rows, task='speech', mode='automatic',
                              metric='first_audio_ms', providers=providers),
        speech_first_audio=rank(rows, task='speech', mode='automatic',
                               metric='first_audio_ms', providers=providers),
        speech_completed=rank(rows, task='speech', mode='automatic',
                             metric='completed_ms', providers=providers),
        interpretation=['first_audio_is_decoded_or_received_speech_not_soundcard_playout',
            'three_repeats_are_descriptive_not_statistical_or_field_acceptance',
            'connection_and_start_api_latency_are_excluded_from_turn_latency',
            'different_native_and_websocket_clients_include_different_codec_buffering',
            *(['archived_Tencent_receipts_are_read_only_no_live_driver',
               'Qwen_and_Doubao_confirm_20ms_RMS_Tencent_confirms_40ms_RMS_and_reports_first_window']
              if archived else ['Qwen_and_Doubao_confirm_20ms_RMS']),
            'exact_wording_checks_cloud_subtitles_not_an_independent_transcription_of_received_audio',
            'no_route_has_completed_the_real_aircraft_scenario'])


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--folder', type=Path, default=DEFAULT)
    parser.add_argument('--output', type=Path)
    args=parser.parse_args()
    value=compare(args.folder)
    target=args.output or args.folder/'Summary.json'
    if target.resolve().is_relative_to(ARCHIVE.resolve()):
        raise ValueError('archived_evidence_is_read_only_specify_output_outside_archive')
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({'summary_file':str(target.resolve()),'capabilities':value['capabilities'],
                      'media_first_audio':value['media_first_audio'],
                      'strict_wording_ranking':value['speech_first_audio'],
                      'delivery_summary':value['delivery_summary']}, ensure_ascii=True))
