# 商品登録ワークフロー ステートマシン図

## 目的

1. **全パスの洗い出し**: 正常パス・エラーパス・スキップパスを明確化
2. **システムレベルの整合性チェック**: 依存関係の可視化と検証
3. **設計品質向上**: コード実装前の仕様確定

---

## 1. メインステートマシン（全体フロー）

```mermaid
stateDiagram-v2
    [*] --> IDLE

    IDLE --> STEP1_GENERATING: start_registration(input_data)

    state "STEP 1: 原稿生成" as STEP1_GENERATING {
        [*] --> Generating
        Generating --> ValidatingResult
        ValidatingResult --> [*]: ppv_id & menu_id OK
        ValidatingResult --> Generating: retry (max 3)
    }

    STEP1_GENERATING --> STEP1_COMPLETED: success
    STEP1_GENERATING --> STEP1_FAILED: max_retry_exceeded

    STEP1_COMPLETED --> STEP2_REGISTERING: ppv_id, menu_id, subtitles

    state "STEP 2: メニュー登録" as STEP2_REGISTERING {
        [*] --> LoginCMS
        LoginCMS --> SelectSite
        SelectSite --> InputManuscript
        InputManuscript --> UploadManuscript
        UploadManuscript --> VerifyUpload
        VerifyUpload --> [*]: ppv_id in UP済み一覧
    }

    STEP2_REGISTERING --> STEP2_COMPLETED: success
    STEP2_REGISTERING --> STEP2_FAILED: error

    STEP2_COMPLETED --> STEP3_SETTING_PPV: menu_id, session

    state "STEP 3: PPV情報登録" as STEP3_SETTING_PPV {
        [*] --> NavigateCmsPpv
        NavigateCmsPpv --> InputPriceGuide
        InputPriceGuide --> InputCarrierValues
        InputCarrierValues --> InputYudoSettings
        InputYudoSettings --> SavePpvInfo
        SavePpvInfo --> VerifyPpvSaved
        VerifyPpvSaved --> [*]: status = 登録済み
    }

    STEP3_SETTING_PPV --> STEP3_COMPLETED: success
    STEP3_SETTING_PPV --> STEP3_FAILED: error

    note right of STEP3_COMPLETED
        ⚠️ 重要: STEP3完了が
        STEP4実行の前提条件
    end note

    STEP3_COMPLETED --> STEP4_SETTING_MENU: [STEP3 = 登録済み]

    state "STEP 4: メニュー設定" as STEP4_SETTING_MENU {
        [*] --> NavigateCmsMenu
        NavigateCmsMenu --> DeterminePrefix
        DeterminePrefix --> SetDisplayFlag: all prefixes
        SetDisplayFlag --> SetStrokeCount: monthlyAffinity only
        SetDisplayFlag --> SaveMenuSettings: fixedCode only
        SetStrokeCount --> SetZokkanSettings
        SetZokkanSettings --> SaveMenuSettings
        SaveMenuSettings --> VerifyMenuSaved
        VerifyMenuSaved --> [*]: inputs[21] = 1
    }

    STEP4_SETTING_MENU --> STEP4_COMPLETED: success
    STEP4_SETTING_MENU --> STEP4_FAILED: error (STEP3未完了の可能性)

    STEP4_COMPLETED --> STEP5_UPLOADING_CSV

    state "STEP 5: MKB CSVアップロード" as STEP5_UPLOADING_CSV {
        [*] --> CheckVPN
        CheckVPN --> LoginMKB: VPN OK
        CheckVPN --> VPN_ERROR: VPN NG
        LoginMKB --> DownloadCSV
        DownloadCSV --> FixPublicDate
        FixPublicDate --> SelectSiteUpload
        SelectSiteUpload --> UploadCSV
        UploadCSV --> VerifyImport
        VerifyImport --> [*]: X件保存しました
    }

    STEP5_UPLOADING_CSV --> STEP5_COMPLETED: success
    STEP5_UPLOADING_CSV --> STEP5_SKIPPED: VPN_ERROR (continue with warning)
    STEP5_UPLOADING_CSV --> STEP5_FAILED: error

    STEP5_COMPLETED --> STEP6_SYNCING
    STEP5_SKIPPED --> STEP6_SYNCING: ⚠️ 警告付き続行

    state "STEP 6: izumo同期" as STEP6_SYNCING {
        [*] --> NavigateIzumoAdmin
        NavigateIzumoAdmin --> ExecuteSync
        ExecuteSync --> WaitSyncComplete
        WaitSyncComplete --> VerifySync
        VerifySync --> [*]: 同期完了 & ppv_id exists
    }

    STEP6_SYNCING --> STEP6_COMPLETED: success
    STEP6_SYNCING --> STEP6_FAILED: error

    STEP6_COMPLETED --> STEP7_REFLECTING: [同期完了]

    state "STEP 7: 小見出し反映" as STEP7_REFLECTING {
        [*] --> NavigateMenuHtml
        NavigateMenuHtml --> FilterMenuId
        FilterMenuId --> ExecuteReflect
        ExecuteReflect --> WaitReflectComplete
        WaitReflectComplete --> VerifyReflect
        VerifyReflect --> [*]: 反映完了 & status = 反映済み
    }

    STEP7_REFLECTING --> STEP7_COMPLETED: success
    STEP7_REFLECTING --> STEP7_FAILED: error

    STEP7_COMPLETED --> STEP8_UPDATING: [反映完了]

    state "STEP 8: 従量自動更新" as STEP8_UPDATING {
        [*] --> NavigateAutomationEdit
        NavigateAutomationEdit --> SelectPpvId
        SelectPpvId --> DetermineTheme
        DetermineTheme --> SetPublicDate
        SetPublicDate --> SelectThemeCheckbox
        SelectThemeCheckbox --> SaveAutomation
        SaveAutomation --> VerifyAutomation
        VerifyAutomation --> [*]: ppv_id in 自動更新一覧
    }

    STEP8_UPDATING --> COMPLETED: success
    STEP8_UPDATING --> STEP8_FAILED: error

    COMPLETED --> [*]

    %% エラー状態
    STEP1_FAILED --> [*]: 中断（必須STEP）
    STEP2_FAILED --> [*]: 中断（必須STEP）
    STEP3_FAILED --> [*]: 中断推奨（STEP4に影響）
    STEP4_FAILED --> STEP5_UPLOADING_CSV: ⚠️ 警告付き続行可
    STEP5_FAILED --> STEP6_SYNCING: ⚠️ 警告付き続行可
    STEP6_FAILED --> STEP7_REFLECTING: ⚠️ 警告付き続行可
    STEP7_FAILED --> STEP8_UPDATING: ⚠️ 警告付き続行可
    STEP8_FAILED --> [*]: ⚠️ 警告付き終了
```

---

## 2. 依存関係グラフ（DAG）

```mermaid
graph TB
    subgraph "Phase 1: 原稿生成"
        S1[STEP 1<br/>原稿生成・PPV ID発行]
    end

    subgraph "Phase 2: 原稿管理CMS"
        S2[STEP 2<br/>メニュー登録]
        S3[STEP 3<br/>PPV情報登録]
        S4[STEP 4<br/>メニュー設定]
    end

    subgraph "Phase 3: 外部システム"
        S5[STEP 5<br/>MKB CSVアップロード]
    end

    subgraph "Phase 4: izumo CMS"
        S6[STEP 6<br/>izumo同期]
        S7[STEP 7<br/>小見出し反映]
        S8[STEP 8<br/>従量自動更新]
    end

    S1 -->|ppv_id, menu_id, subtitles| S2
    S2 -->|menu_id確定, session維持| S3
    S3 -->|"⚠️ 登録済み必須"| S4
    S4 -->|原稿管理CMS完了| S5
    S5 -->|"(VPN必須)"| S6
    S6 -->|"⚠️ 同期完了必須"| S7
    S7 -->|"⚠️ 反映完了必須"| S8

    %% 強い依存（スキップ不可）
    linkStyle 0 stroke:red,stroke-width:2px
    linkStyle 1 stroke:red,stroke-width:2px
    linkStyle 2 stroke:red,stroke-width:3px
    linkStyle 5 stroke:red,stroke-width:2px
    linkStyle 6 stroke:red,stroke-width:2px

    %% 弱い依存（スキップ可能）
    linkStyle 3 stroke:orange,stroke-width:1px
    linkStyle 4 stroke:orange,stroke-width:1px

    %% 凡例
    subgraph Legend
        L1[強依存<br/>スキップ不可]
        L2[弱依存<br/>警告付き続行可]
    end

    style L1 fill:#ffcccc,stroke:red
    style L2 fill:#fff3cd,stroke:orange
```

---

## 3. システム境界とセッション管理

```mermaid
graph LR
    subgraph "ローカル環境"
        API[Rohan API<br/>localhost:5558]
    end

    subgraph "原稿管理CMS"
        direction TB
        CMS_LOGIN[ログイン]
        CMS_SITE[サイト選択]
        CMS_PPV[?p=cms_ppv]
        CMS_MENU[?p=cms_menu]
        CMS_UP[?p=up]

        CMS_LOGIN --> CMS_SITE
        CMS_SITE --> CMS_PPV
        CMS_PPV --> CMS_MENU
        CMS_MENU --> CMS_UP
    end

    subgraph "MKB"
        MKB_LOGIN[ログイン]
        MKB_CSV[CSV Import]

        MKB_LOGIN --> MKB_CSV
    end

    subgraph "izumo CMS"
        direction TB
        IZM_ADMIN[管理画面]
        IZM_SYNC[同期]
        IZM_MENU[menu.html]
        IZM_AUTO[自動更新登録]

        IZM_ADMIN --> IZM_SYNC
        IZM_SYNC --> IZM_MENU
        IZM_MENU --> IZM_AUTO
    end

    API --> CMS_LOGIN
    CMS_UP --> MKB_LOGIN
    MKB_CSV --> IZM_ADMIN

    %% セッション境界
    style API fill:#e1f5fe
    style CMS_LOGIN fill:#fff3e0
    style CMS_SITE fill:#fff3e0
    style CMS_PPV fill:#fff3e0
    style CMS_MENU fill:#fff3e0
    style CMS_UP fill:#fff3e0
    style MKB_LOGIN fill:#f3e5f5
    style MKB_CSV fill:#f3e5f5
    style IZM_ADMIN fill:#e8f5e9
    style IZM_SYNC fill:#e8f5e9
    style IZM_MENU fill:#e8f5e9
    style IZM_AUTO fill:#e8f5e9
```

### セッション管理ルール

| フェーズ | システム | セッション | 並列実行 |
|---------|---------|-----------|---------|
| Phase 1 | Rohan API | なし（ステートレス） | - |
| Phase 2 | 原稿管理CMS | STEP 2-4で維持 | **禁止** |
| Phase 3 | MKB | 独立セッション | 理論上可能 |
| Phase 4 | izumo CMS | STEP 6-8で維持 | **禁止** |

---

## 4. ガード条件（遷移条件）

```mermaid
stateDiagram-v2
    state guard_3_to_4 <<choice>>
    state guard_5_vpn <<choice>>
    state guard_6_to_7 <<choice>>
    state guard_7_to_8 <<choice>>

    STEP3_COMPLETED --> guard_3_to_4
    guard_3_to_4 --> STEP4_SETTING_MENU: [cms_ppv.status == "登録済み"]
    guard_3_to_4 --> STEP3_RETRY: [cms_ppv.status == "未登録"]

    STEP4_COMPLETED --> guard_5_vpn
    guard_5_vpn --> STEP5_UPLOADING_CSV: [VPN接続 == true]
    guard_5_vpn --> STEP5_SKIPPED: [VPN接続 == false] ⚠️

    STEP5_COMPLETED --> guard_6_to_7
    STEP5_SKIPPED --> guard_6_to_7
    guard_6_to_7 --> STEP6_SYNCING

    STEP6_COMPLETED --> guard_6_to_7_2
    state guard_6_to_7_2 <<choice>>
    guard_6_to_7_2 --> STEP7_REFLECTING: [snapshot.includes("同期完了")]
    guard_6_to_7_2 --> STEP6_RETRY: [else]

    STEP7_COMPLETED --> guard_7_to_8
    guard_7_to_8 --> STEP8_UPDATING: [snapshot.includes("反映完了")]
    guard_7_to_8 --> STEP7_RETRY: [else]
```

---

## 5. エラーハンドリングマトリクス

| STEP | 失敗条件 | 対処 | 続行可否 | 備考 |
|------|---------|------|---------|------|
| **1** | API エラー / 生成失敗 | リトライ (max 3) | **中断** | 原稿がないと続行不可 |
| **2** | ログイン失敗 / 登録エラー | 認証確認、リトライ | **中断** | CMS登録必須 |
| **3** | 保存失敗 / セッション切れ | 再ログイン、リトライ | **中断推奨** | STEP4に影響 |
| **4** | `{status: error}` | **STEP3が未完了** → 戻る | 警告付き続行 | 後で手動設定可 |
| **5** | VPN未接続 / CSV形式エラー | VPN確認、CSV修正 | 警告付き続行 | 後で手動実行可 |
| **6** | 認証失敗 / 同期エラー | URL再構成、リトライ | 警告付き続行 | 後で手動同期可 |
| **7** | menu_id未発見 / 反映エラー | STEP6確認、リトライ | 警告付き続行 | 後で手動反映可 |
| **8** | ppv_id選択不可 / 登録エラー | STEP6,7確認 | 警告付き終了 | オプション処理 |

### クリティカルパス（失敗で必ず中断）

```
STEP 1 → STEP 2 → STEP 3
```

### ベストエフォートパス（警告付き続行可能）

```
STEP 4 → STEP 5 → STEP 6 → STEP 7 → STEP 8
```

---

## 6. 検証チェックリスト（各STEP完了条件）

```mermaid
graph TD
    subgraph "STEP 1 検証"
        V1_1[ppv_id が 8桁数字]
        V1_2[menu_id が prefix + number.subtitle 形式]
        V1_3[subtitles 件数 = 入力件数]
        V1_4[各 subtitle に body あり]
    end

    subgraph "STEP 2 検証"
        V2_1[ppv_id が UP済み一覧に表示]
        V2_2[ステータス = 登録済み or UP済み]
        V2_3[小見出し数 = 期待値]
    end

    subgraph "STEP 3 検証"
        V3_1["price = 設定値 (例: 2000)"]
        V3_2[guide が空でない]
        V3_3[affinity = 0 or 1]
        V3_4[dmenu_sid = 00073734509]
        V3_5[yudo_ppv_id_01 が設定済み]
    end

    subgraph "STEP 4 検証"
        V4_1["inputs[21] = 1 (表示フラグ)"]
        V4_2["monthlyAffinity: inputs[40] = 2"]
        V4_3["保存結果 ≠ {status: error}"]
    end

    subgraph "STEP 5 検証"
        V5_1["「X件保存しました」メッセージ"]
        V5_2[public_date エラーなし]
    end

    subgraph "STEP 6 検証"
        V6_1["「同期完了」メッセージ"]
        V6_2[ppv_id が ppv一覧に存在]
    end

    subgraph "STEP 7 検証"
        V7_1["「反映完了」メッセージ"]
        V7_2[menu_id が検索結果に存在]
        V7_3[ステータス = 反映済み]
    end

    subgraph "STEP 8 検証"
        V8_1["「登録しました」メッセージ"]
        V8_2[ppv_id が自動更新一覧に存在]
        V8_3[テーマ = カテゴリコード対応値]
    end
```

---

## 7. menu_idプレフィックス別の分岐

```mermaid
flowchart TD
    START[menu_id取得] --> CHECK{プレフィックス判定}

    CHECK -->|fixedCode*| FIXED[fixedCode系]
    CHECK -->|monthlyAffinity*| MONTHLY[monthlyAffinity系]
    CHECK -->|その他| DEFAULT[デフォルト]

    FIXED --> FIXED_SET["STEP4: 表示フラグのみ<br/>inputs[21] = 1"]
    MONTHLY --> MONTHLY_SET["STEP4: 全項目設定<br/>inputs[21] = 1<br/>inputs[40] = 2<br/>inputs[94] = 1<br/>inputs[95] = 1<br/>inputs[101] = 1<br/>inputs[102] = 1"]
    DEFAULT --> MONTHLY_SET

    FIXED_SET --> CONTINUE[STEP5へ]
    MONTHLY_SET --> CONTINUE
```

---

## 8. カテゴリコード → テーマIDマッピング（STEP 8用）

```mermaid
graph LR
    subgraph "カテゴリコード"
        C02[02: あの人の気持ち]
        C03[03: 相性]
        C04[04: 片想い]
        C05[05: 恋の行方]
        C06[06: 秘密の恋]
        C07[07: 復縁]
        C08[08: 夜の相性]
        C09[09: 豪華恋愛]
        C10[10: 恋愛パック]
        C11[11: 結婚]
        C12[12: 出逢い]
        C13[13: そばにある恋]
        C14[14: 豪華結婚]
        C15[15: 豪華出逢い]
        C16[16: 人生]
        C17[17: 仕事]
        C18[18: 豪華人生]
        C19[19: 人生パック]
        C20[20: 年運]
    end

    subgraph "テーマID"
        T1["テーマ1: あの人との恋"]
        T2["テーマ2: 複雑な恋"]
        T3["テーマ3: 出逢いと結婚"]
        T4["テーマ4: 人生と仕事"]
    end

    C02 --> T1
    C03 --> T1
    C04 --> T1
    C05 --> T1
    C08 --> T1
    C09 --> T1
    C10 --> T1

    C06 --> T2
    C07 --> T2

    C11 --> T3
    C12 --> T3
    C13 --> T3
    C14 --> T3
    C15 --> T3

    C16 --> T4
    C17 --> T4
    C18 --> T4
    C19 --> T4
    C20 --> T4
```

---

## 9. 使用方法

### 整合性チェック

このステートマシン図を使用して以下をチェック：

1. **パス網羅**: すべての遷移パスが存在するか
2. **デッドロック検出**: 遷移できない状態がないか
3. **ガード条件漏れ**: 条件分岐が明確か
4. **エラーハンドリング網羅**: すべてのエラー状態に対処があるか

### コード実装時の参照

```javascript
// ステートマシン実装例
const registrationStateMachine = {
    initial: 'IDLE',
    states: {
        IDLE: { on: { START: 'STEP1_GENERATING' } },
        STEP1_GENERATING: {
            on: {
                SUCCESS: 'STEP1_COMPLETED',
                FAILURE: 'STEP1_FAILED'
            }
        },
        STEP1_COMPLETED: { on: { NEXT: 'STEP2_REGISTERING' } },
        // ... 以下同様
    },
    guards: {
        'step3Registered': (context) => context.cms_ppv_status === '登録済み',
        'vpnConnected': (context) => context.vpn_status === true,
        'syncCompleted': (context) => context.snapshot.includes('同期完了'),
        'reflectCompleted': (context) => context.snapshot.includes('反映完了')
    }
};
```

---

## 更新履歴

| 日付 | 変更内容 |
|------|---------|
| 2026-01-28 | 初版作成。全STEPのステートマシン図を追加 |
