#!/bin/bash
# 中文字幕烧录验证脚本（在容器内跑）
printf '1\n00:00:00,000 --> 00:00:03,000\n海南清补凉真好吃，欢迎来到海口\n2\n00:00:03,000 --> 00:00:06,000\n第二行中文测试字幕\n' > /tmp/test.srt
ffmpeg -y -f lavfi -i color=c=black:s=640x360:d=6 -vf "subtitles=/tmp/test.srt:force_style='FontName=Noto Sans CJK SC,FontSize=24'" -c:v libx264 /tmp/subtest.mp4 2>&1 | grep -iE 'fontselect|error|failed|warn' | head -5
echo "--- 输出文件 ---"
ls -la /tmp/subtest.mp4
echo "--- 提取一帧查看字幕渲染 ---"
ffmpeg -y -i /tmp/subtest.mp4 -ss 1 -frames:v 1 /tmp/subframe.png 2>/dev/null && ls -la /tmp/subframe.png
