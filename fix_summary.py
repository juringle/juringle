with open("daily_summary.py", "r") as f:
    content = f.read()

old = '''        코멘트_short = 코멘트.split('.')[0].strip() if 코멘트 else ''
        if len(코멘트_short) > 25:
            코멘트_short = 코멘트_short[:25] + '...' '''

new = '''        # 코멘트에서 핵심 한 줄 추출 (기업소개 제외, 투자의견 중심)
        lines = [l.strip() for l in 코멘트.split('\\n') if l.strip()]
        # 두번째 줄 (투자 근거) 우선 사용
        if len(lines) >= 2:
            코멘트_short = lines[1].lstrip('•').strip()
        elif lines:
            코멘트_short = lines[0].lstrip('•').strip()
        else:
            코멘트_short = 코멘트.split('.')[0].strip() if 코멘트 else ''
        if len(코멘트_short) > 35:
            코멘트_short = 코멘트_short[:35] + '...' '''

if old in content:
    content = content.replace(old, new)
    with open("daily_summary.py", "w") as f:
        f.write(content)
    print("✅ 수정 완료!")
else:
    print("❌ 코드를 찾지 못했어.")
