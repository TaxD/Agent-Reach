"""Install receipt and capability summary. No I/O: management passes in the status records."""
from datetime import datetime

_CAPABILITY_LIMITS = {
    'youtube': ['read는 메타 정보와 설명만 반환합니다. 독립된 comments 동작은 없습니다.',
                '자막은 영상에 자막 트랙이 있어야 하며, 전사는 별도로 API 키와 사용량이 필요합니다.'],
    'reddit': ['로그인 없이 공개 웹 검색과 페이지 읽기로만 접근합니다. Reddit이 요청을 차단하면 본문과 댓글을 읽지 못할 수 있습니다.'],
    'twitter': ['로그인 없이 트윗 하나를 읽는 동작만 제공합니다. 검색, 타임라인, 답글 스레드는 제공하지 않습니다.'],
    'github-trending': ['공식 API가 없어서 트렌딩 페이지의 HTML을 해석합니다. 페이지 구조가 바뀌면 일시적으로 실패할 수 있습니다.'],
    'dribbble': ['공개 API가 없어서 웹 검색과 페이지 읽기로만 접근합니다. 이미지 자체는 반환하지 않습니다.'],
    'producthunt': ['공개 피드에 실린 최신 제품만 제공합니다. 득표 수와 검색은 제공하지 않습니다.'],
    'medium': ['피드는 최근 글 약 10개만 제공하며, 유료 글은 본문 일부만 읽힐 수 있습니다.'],
    'rss': ['공개 인터넷 주소의 피드만 읽습니다. 사설망·로컬 주소는 차단됩니다.'],
}

_DISPLAY_NAMES = {
    'github': 'GitHub', 'github-trending': 'GitHub Trending', 'youtube': 'YouTube', 'web': '웹',
    'reddit': 'Reddit', 'twitter': 'X / Twitter', 'hackernews': 'Hacker News',
    'stackoverflow': 'Stack Overflow', 'lobsters': 'Lobsters', 'devto': 'dev.to', 'mdn': 'MDN',
    'npm': 'npm', 'producthunt': 'Product Hunt', 'medium': 'Medium', 'dribbble': 'Dribbble',
    'bluesky': 'Bluesky', 'rss': 'RSS/피드', 'geeknews': 'GeekNews', 'yozm': '요즘IT',
}

_STATUS_LABELS = {
    'not_added': '미연결(필요할 때 추가)', 'needs_setup': '설정 필요', 'unverified': '연결됨(실측 전)',
    'ready': '실측 통과', 'search_ready': '검색만 실측 통과',
    'cooldown': '일시 중지(요청 제한)',
    'needs_user_action': '사용자 인증 대기', 'broken': '현재 경로 장애',
}


def _local_greeting():
    """Use the installing computer's clock and timezone, without a network lookup."""
    hour = datetime.now().hour
    if 5 <= hour < 11:
        return '좋은 아침입니다. 무엇을 찾아드릴까요?'
    if 11 <= hour < 18:
        return '안녕하세요. 무엇을 찾아드릴까요?'
    return '좋은 저녁입니다. 무엇을 찾아드릴까요?'


def installation_report(result):
    """Render one short receipt; keep detailed evidence in the structured summary."""
    def cell(value):
        return str(value).replace('|', r'\|').replace('\n', ' ')

    entries = result['capability_summary']['channels']
    installs = {row['channel']: row for row in result['default_channels']}
    installed = sum(bool(row['ok']) for row in installs.values())
    completed = all(row['ok'] for row in installs.values())
    lines = [
        _local_greeting(), '',
        f"**Agent Reach {result['version']} {'설치가 완료되었습니다' if completed else '설치가 아직 끝나지 않았습니다'}.** "
        f'기본 채널 {len(installs)}개 중 {installed}개가 준비되었습니다.', '',
        '모든 채널은 로그인 없이 공개 데이터만 읽습니다.', '',
        '| 플랫폼 | 용도 |', '|---|---|',
    ]
    for entry in sorted(entries, key=lambda item: item['level'] != 'default'):
        name = entry['channel']
        label = _DISPLAY_NAMES.get(name, name)
        item = installs.get(name)
        if entry['level'] == 'default':
            if item and not item['ok']:
                label += ' (설치 실패: ' + item.get('error', {}).get('code', 'UNKNOWN') + ')'
            elif not item:
                label += ' (사용자가 제거함)'
        elif entry['reach_status'] in {'needs_setup', 'cooldown', 'needs_user_action', 'broken'}:
            label += ' (' + _STATUS_LABELS[entry['reach_status']] + ')'
        lines.append('| ' + ' | '.join(cell(value) for value in (label, entry['purpose'])) + ' |')
    verified = [entry for entry in entries if entry['verified_actions']]
    if verified:
        names = '; '.join(_DISPLAY_NAMES.get(entry['channel'], entry['channel']) + ': ' +
                          '/'.join(entry['verified_actions']) for entry in verified)
        lines += ['', '실측 통과: ' + names + '. 나머지는 아직 항목별로 실측하지 않았습니다.']
    else:
        lines += ['', '아직 항목별 실측은 하지 않았습니다.']
    lines += ['', '**찾고 싶은 것을 바로 말씀해 주십시오.**']
    return '\n'.join(lines) + '\n'


def capability_summary(channels, statuses):
    """Describe public options, declared actions and local evidence without any I/O.

    Management supplies status records so this summary cannot initiate discovery,
    installation, login or a live request. Actions always come from the registry.
    """
    by_name = {entry['channel']: entry for entry in statuses}
    entries = []
    for ch in channels:
        current = by_name.get(ch.name, {})
        entries.append({
            'channel': ch.name,
            'level': ch.level,
            'reach_status': current.get('status', 'unverified'),
            'verified_actions': current.get('verified_actions', []),
            'actions': [{'action': action.name, 'detail': action.help} for action in ch.actions],
            'purpose': ch.purpose,
            'limits': _CAPABILITY_LIMITS.get(ch.name, []),
        })
    message = ['모든 채널은 로그인 없이 공개 데이터만 읽습니다. 채널의 용도(purpose)를 보고 질문에 맞는 채널을 고릅니다.']
    for entry in entries:
        actions = '/'.join(action['action'] for action in entry['actions'])
        status_label = _STATUS_LABELS.get(entry['reach_status'], entry['reach_status'])
        limits = ' '.join(entry['limits'])
        message.append(f"{entry['channel']}: {entry['purpose']} 동작 {actions} ({status_label}). {limits}".rstrip())
    return {
        'policy': 'public_only; no_login',
        'evidence_note': '용도 설명은 선언된 기능이며, 이 컴퓨터에서 실측한 결과를 대신하지 않습니다.',
        'channels': entries,
        'message': '\n'.join(message),
    }
