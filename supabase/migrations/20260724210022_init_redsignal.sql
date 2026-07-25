-- RedSignal Supabase schema
-- 字段严格对照 backend/models.py 与 backend/store.py 的内存结构。
--
-- 权限模型：后端 FastAPI 持 service_role 全权写入；前端只通过 WebSocket 拿数据，
-- 不直连 Supabase。因此所有表 enable RLS 且**不建 anon policy**（anon 完全无权），
-- 这是最省事也最安全的默认。日后若前端要直连 Realtime，再逐表开 select。
--
-- 明确不入库：IMU 原始批次（models.py::IMUBatch "只留内存，不入库"）。

-- ---------------------------------------------------------------- 活动
create table if not exists public.events (
  event_id    text primary key,           -- config.DEFAULT_EVENT_ID = 'adventurex_2026'
  name        text not null,
  starts_at   timestamptz,
  ends_at     timestamptz,
  created_at  timestamptz not null default now()
);

-- ------------------------------------------------- 用户在某场活动的档案
-- 对应 UserEventProfile + store.states + store.user_quiet_until
create table if not exists public.user_event_profiles (
  user_id             text not null,
  event_id            text not null references public.events(event_id) on delete cascade,
  nickname            text not null,
  mode                text not null default 'off'
                        check (mode in ('love','friend','off')),
  social_goal         text not null,
  communication_style text not null,
  interest_tags       text[] not null default '{}',   -- 用户原始输入
  normalized_tags     text[] not null default '{}',   -- tags.normalize_tags() 结果
  share_bundle        jsonb  not null default '{}'::jsonb,
  state               text   not null default 'BLUE_OFFLINE'
                        check (state in ('BLUE_OFFLINE','DISCOVERABLE','CANDIDATE_NEARBY',
                                         'NOTIFIED','SELF_CONFIRMED','ENCOUNTER_CONFIRMED',
                                         'CONNECTED','CONTENT_READY','CANCELLED')),
  quiet_until         timestamptz,        -- 取代 store.user_quiet_until（monotonic → 绝对时间）
  expires_at          timestamptz,
  updated_at          timestamptz not null default now(),
  primary key (user_id, event_id),

  -- DB 层兜底 models.FORBIDDEN_FIELDS，即使后端有 bug 也存不进去
  constraint no_forbidden_fields check (
    not (share_bundle ?| array['phone','real_name','precise_location','health','raw_audio'])
  )
);
create index if not exists profiles_tags_idx
  on public.user_event_profiles using gin (normalized_tags);
create index if not exists profiles_discoverable_idx
  on public.user_event_profiles (event_id, mode) where mode <> 'off';

-- 状态机变更审计（可选，但排障时很值）
create table if not exists public.state_transitions (
  id          bigint generated always as identity primary key,
  user_id     text not null,
  event_id    text not null,
  from_state  text,
  to_state    text not null,
  reason      text,
  created_at  timestamptz not null default now()
);

-- ---------------------------------------------------------------- 拉黑
-- 取代 UserEventProfile.blocked_users
create table if not exists public.blocks (
  event_id        text not null,
  user_id         text not null,
  blocked_user_id text not null,
  created_at      timestamptz not null default now(),
  primary key (event_id, user_id, blocked_user_id)
);

-- ------------------------------------------------------ 轮转匿名 ID
-- 取代 store.ephemeral_map
create table if not exists public.ephemeral_ids (
  ephemeral_id text primary key,
  user_id      text not null,
  event_id     text not null,
  issued_at    timestamptz not null default now(),
  expires_at   timestamptz
);
create index if not exists ephemeral_user_idx on public.ephemeral_ids (user_id);

-- ------------------------------------------------------------ BLE 观测
-- 高频写入。热路径建议仍走内存 deque，这里只做异步落盘用于调参/复盘。
create table if not exists public.sightings (
  id                bigint generated always as identity primary key,
  observer_user_id  text not null,
  ephemeral_id      text not null,
  rssi              smallint not null,
  seen_at           timestamptz not null default now()
);
create index if not exists sightings_lookup_idx
  on public.sightings (observer_user_id, ephemeral_id, seen_at desc);

-- ---------------------------------------------------------- 候选人对
-- 对应 CandidatePair；pair_key 同时取代 store.pair_tried_at（frozenset 冷却）
create table if not exists public.candidate_pairs (
  pair_id               text primary key,
  event_id              text not null,
  user_a                text not null,
  user_b                text not null,
  mode                  text not null check (mode in ('love','friend')),
  match_score           smallint not null,
  score_breakdown       jsonb,          -- matching.score_breakdown()，保留可解释性
  proximity_band        text check (proximity_band in ('very_near','near','far')),
  created_at            timestamptz not null default now(),
  candidate_expires_at  timestamptz,
  cancelled             boolean not null default false,
  cancel_reason         text,
  -- 无序对规范键：{a,b} 与 {b,a} 折叠成同一个值
  pair_key text generated always as
    (least(user_a, user_b) || '|' || greatest(user_a, user_b)) stored
);
create index if not exists pairs_cooldown_idx
  on public.candidate_pairs (event_id, pair_key, created_at desc);
create index if not exists pairs_live_a_idx
  on public.candidate_pairs (user_a) where not cancelled;
create index if not exists pairs_live_b_idx
  on public.candidate_pairs (user_b) where not cancelled;

-- ------------------------------------------------------ 戒指确认事件
-- 双方同意的证据链，必须留档。unique 约束顺手提供幂等（同一 pair 同一人只记一次）
create table if not exists public.ring_button_events (
  id          bigint generated always as identity primary key,
  pair_id     text not null references public.candidate_pairs(pair_id) on delete cascade,
  user_id     text not null,
  event_type  text not null
                check (event_type in ('double_press_confirm','double_tap')),
  device_id   text not null default 'mock',
  detected_at timestamptz not null default now(),
  unique (pair_id, user_id, event_type)
);

-- ------------------------------------------------------------ 已成连接
create table if not exists public.encounters (
  encounter_id        text primary key,
  pair_id             text not null unique references public.candidate_pairs(pair_id),
  confirmed_by        text[] not null,
  confirmation_method text not null
                        check (confirmation_method in ('dual_ring_button','app_double_confirm')),
  shared_fields       jsonb not null,   -- {接收方user_id: 其可见的对方卡片}
  optional_gesture    text,
  agent_content       jsonb,            -- Agent 破冰内容，可为 null（生成失败不阻断交换）
  created_at          timestamptz not null default now()
);

-- -------------------------------------------------- 可穿戴设备最新快照
-- 对应 wearable_hub.UnifiedDeviceSnapshot.to_dict()，整体 upsert
create table if not exists public.device_status (
  user_id    text primary key,
  ring       jsonb not null default '{}'::jsonb,
  watch      jsonb not null default '{}'::jsonb,
  updated_at timestamptz not null default now()
);

-- 手表健康时间序列（可选）。注意：health 属于 FORBIDDEN_FIELDS，
-- 只允许本人可见，绝不可进入 share_bundle / encounters.shared_fields。
create table if not exists public.watch_health_samples (
  id          bigint generated always as identity primary key,
  user_id     text not null,
  metric      text not null
                check (metric in ('heart_rate','steps','spo2','stress','sleep','battery')),
  value       real not null,
  recorded_at timestamptz not null default now()
);
create index if not exists watch_samples_idx
  on public.watch_health_samples (user_id, metric, recorded_at desc);

-- ------------------------------------------- 标签词表（可选：现场加词免重启）
create table if not exists public.tag_synonyms (
  variant   text primary key,     -- 小写等价写法
  canonical text not null
);
create table if not exists public.tag_domains (
  canonical text primary key,
  domain    text not null         -- ai | build | music | culture | active | craft
);

-- ---------------------------------------------------------------- RLS
alter table public.events                enable row level security;
alter table public.user_event_profiles   enable row level security;
alter table public.state_transitions     enable row level security;
alter table public.blocks                enable row level security;
alter table public.ephemeral_ids         enable row level security;
alter table public.sightings             enable row level security;
alter table public.candidate_pairs       enable row level security;
alter table public.ring_button_events    enable row level security;
alter table public.encounters            enable row level security;
alter table public.device_status         enable row level security;
alter table public.watch_health_samples  enable row level security;
alter table public.tag_synonyms          enable row level security;
alter table public.tag_domains           enable row level security;
-- 不建任何 anon policy = anon key 读不到任何东西；service_role 绕过 RLS。

-- ---------------------------------------------------------------- 种子
insert into public.events (event_id, name)
values ('adventurex_2026', 'AdventureX 2026')
on conflict (event_id) do nothing;
