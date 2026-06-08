import re
with open('/Users/bilibili/Desktop/J-TradingAgents/_top500_and_leaders.txt', 'r') as f:
    lines = f.readlines()
count = 0
for line in lines:
    if re.match(r'\s*\d+\.\s+\d{6}\s+', line) and '[DONE]' not in line:
        m = re.match(r'\s*(\d+)\.\s+(\d{6})\s+(\S+)', line)
        if m:
            print(f'{m.group(2)} {m.group(3)} (line {m.group(1)})')
            count += 1
            if count >= 5:
                break
