
    // ================= 群发功能 =================
    let cbImages = [];   // 群发栏待发图片 [{name, data_url}]

    // 渲染群发栏图片缩略图
    function renderCbThumbs() {
        const box = $('cbThumbs');
        if (!box) return;
        box.innerHTML = '';
        if (!cbImages.length) { box.style.display = 'none'; return; }
        box.style.display = 'flex';
        cbImages.forEach((img, i) => {
            const thumb = el('div', 'cb-thumb');
            const im = el('img'); im.src = img.data_url;
            const x = el('span', 'cb-thumb-x', '×');
            x.addEventListener('click', () => { cbImages.splice(i, 1); renderCbThumbs(); });
            thumb.appendChild(im); thumb.appendChild(x);
            box.appendChild(thumb);
        });
    }

    // 更新群发目标提示文字
    function updateCbTarget() {
        const target = $('cbTarget');
        if (!target) return;
        const visible = tasks.filter(t => t.status !== 'removed' && !t.detached);
        const n = visible.length;
        if (n === 0) { target.textContent = '⚠️ 大屏上没有模型，请先从左侧点击添加'; }
        else { target.textContent = '群发给大屏上的 ' + n + ' 个模型'; }
    }

    // 群发核心：把消息+图片同时发给大屏上所有活跃卡片
    async function broadcastSend() {
        const inp = $('cbInput');
        const message = inp ? inp.value.trim() : '';
        const hasImages = cbImages.length > 0;
        if (!message && !hasImages) return;

        // 筛选大屏上可见且非 running 的卡片
        const visibleTasks = tasks.filter(t => t.status !== 'removed' && !t.detached);
        if (visibleTasks.length === 0) {
            flashTitle('⚠️ 大屏上没有模型，请先从左侧点击添加');
            return;
        }

        // 遍历每个卡片，填入消息并触发发送
        let sentCount = 0;
        for (const task of visibleTasks) {
            if (task.status === 'running') continue;   // 正在跑的跳过

            // 填入文本到卡片的 textarea
            const ta = $('in-' + task.task_id);
            if (ta) {
                ta.value = message;
                autoGrow(ta);
            }

            // 对于 vision 模型，复制群发图片
            if (hasImages && task.vision) {
                task.images = cbImages.map(img => ({ name: img.name, data_url: img.data_url }));
            }

            // 触发该卡片的发送（sendTask 会读 textarea 值和 task.images）
            sendTask(task.task_id);
            sentCount++;
        }

        // 清空群发栏
        if (inp) { inp.value = ''; inp.style.height = 'auto'; inp.style.height = '110px'; }
        cbImages = [];
        renderCbThumbs();

        if (sentCount > 0) {
            flashTitle('📢 已群发给 ' + sentCount + ' 个模型');
        } else {
            flashTitle('所有模型都在运行中，稍后再群发');
        }
        updateCbTarget();
    }

    // 绑定群发栏事件
    (function bindBroadcastEvents() {
        const btn = $('cbSend');
        const inp = $('cbInput');
        const fileInput = $('cbFile');

        // 发送按钮
        if (btn) btn.addEventListener('click', broadcastSend);

        // Enter 发送 / Shift+Enter 换行
        if (inp) inp.addEventListener('keydown', e => {
            if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); broadcastSend(); }
        });

        // 图片上传
        if (fileInput) fileInput.addEventListener('change', () => {
            Array.from(fileInput.files || []).forEach(f => {
                if (!f.type.startsWith('image/')) { flashTitle('只支持图片文件'); return; }
                if (f.size > MAX_IMAGE_BYTES) { flashTitle('单张图片不能超过 5MB'); return; }
                if (cbImages.length >= MAX_IMAGES) { flashTitle('最多 ' + MAX_IMAGES + ' 张图片'); return; }
                const reader = new FileReader();
                reader.onload = () => {
                    cbImages.push({ name: f.name, data_url: reader.result });
                    renderCbThumbs();
                };
                reader.readAsDataURL(f);
            });
            fileInput.value = '';
        });
    })();

