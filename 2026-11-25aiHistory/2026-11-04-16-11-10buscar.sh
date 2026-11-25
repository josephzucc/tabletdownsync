export claude="2026-11-11-11-30-02claudeconversations-000.zip";
export chatgpt="2026-11-18-10-29-03chatgpt.zip";
python3 buscarai.py ${claude} -b "${1}";
python3 buscarai.py ${chatgpt} -b "${1}";
