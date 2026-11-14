"""Remove the repeated-PC DEC alarm scan, as CotT does.

A repeated idle PC is normal. The scan treated MoM colour constants as alarms
and rewrote them with timestamps (confirmed by a watch on 805D9CE4). Deliver
all due DEC interrupts without changing arbitrary SDA RAM or discarding IRQs.
"""
from pathlib import Path
port=Path(__file__).resolve().parents[1]
p=port/'vendor/NWiiRecomp/nWiiRuntime/src/hle/ios.cpp'
text=p.read_text()
marker='NATIVE_DEC_NO_REPEAT_DROP_V1'
if marker not in text:
    start=text.index('      {\n          static uint32_t dec_same_pc_count = 0;')
    end=text.index('      static uint32_t dcount = 0;',start)
    text=text[:start]+'      // '+marker+': deliver every due timer, including repeated idle PCs.\n\n'+text[end:]
assert 'dec_same_pc_count' not in text
assert 'force-expired alarm' not in text
if p.read_text()!=text:p.write_text(text)
print('Repeated-PC DEC interrupts delivered; unsafe alarm scan removed.')
