"""
エクステンション レジストリ

エクステンションの検出・ロード・依存解決・ライフサイクル管理を行う。
"""

import importlib
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Type

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False

logger = logging.getLogger(__name__)


@dataclass
class ExtensionManifest:
    """エクステンション定義マニフェスト。

    manifest.yaml から読み込まれるエクステンションのメタデータ。

    Attributes:
        name: エクステンション一意名（例: "keyword_classifier"）
        version: セマンティックバージョン（例: "1.0.0"）
        tier: 階層（"tier1", "tier2", "tier3"）
        description: 説明文
        entrypoint: エントリポイント（"module.path:ClassName" 形式）
        dependencies: 依存関係（requires: 必須, optional: 任意）
        hooks: フック定義（subscribes: 購読, emits: 発行）
        config: 設定定義（schema: スキーマパス, defaults: デフォルト値）
        contracts: データ契約（consumes: 入力, produces: 出力）
    """
    name: str
    version: str = "0.1.0"
    tier: str = "tier2"
    description: str = ""
    entrypoint: str = ""
    dependencies: Dict[str, List[str]] = field(default_factory=lambda: {
        "requires": [],
        "optional": [],
    })
    hooks: Dict[str, list] = field(default_factory=lambda: {
        "subscribes": [],
        "emits": [],
    })
    config: Dict[str, Any] = field(default_factory=lambda: {
        "schema": "",
        "defaults": {},
    })
    contracts: Dict[str, List[str]] = field(default_factory=lambda: {
        "consumes": [],
        "produces": [],
    })


class Extension(ABC):
    """エクステンション基底クラス。

    すべてのエクステンションはこのクラスを継承し、
    setup / teardown を実装する。
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """エクステンション名を返す。"""

    @abstractmethod
    def setup(self, context: Any) -> None:
        """エクステンションを初期化する。

        Args:
            context: アプリケーションコンテキスト（EventBus, Config 等を含む）
        """

    @abstractmethod
    def teardown(self) -> None:
        """エクステンションをクリーンアップする。"""


class _CycleError(Exception):
    """依存関係の循環を検出した場合のエラー。"""


class ExtensionRegistry:
    """エクステンションの検出・ロード・管理を行うレジストリ。

    Attributes:
        base_path: エクステンション検索のベースディレクトリ
        manifests: 読み込み済みマニフェスト（name -> ExtensionManifest）
        extensions: ロード済みインスタンス（name -> Extension）
    """

    def __init__(self, base_path: Optional[str] = None) -> None:
        """レジストリを初期化する。

        Args:
            base_path: プロジェクトルートパス。省略時はこのファイルの2階層上。
        """
        if base_path is None:
            base_path = str(Path(__file__).resolve().parent.parent)
        self.base_path = base_path
        self.manifests: Dict[str, ExtensionManifest] = {}
        self.extensions: Dict[str, Extension] = {}

    # ------------------------------------------------------------------
    # 検出
    # ------------------------------------------------------------------

    def discover(self, path: str = "extensions") -> List[str]:
        """エクステンション ディレクトリからマニフェストを検索する。

        Args:
            path: base_path からの相対パスまたは絶対パス。

        Returns:
            検出された manifest.yaml のパスリスト。
        """
        ext_dir = Path(path) if os.path.isabs(path) else Path(self.base_path) / path

        if not ext_dir.is_dir():
            logger.warning("エクステンションディレクトリが見つかりません: %s", ext_dir)
            return []

        found: List[str] = []
        for child in sorted(ext_dir.iterdir()):
            if not child.is_dir():
                continue
            manifest_path = child / "manifest.yaml"
            if manifest_path.is_file():
                found.append(str(manifest_path))
                logger.debug("マニフェスト検出: %s", manifest_path)
        return found

    # ------------------------------------------------------------------
    # マニフェスト読込
    # ------------------------------------------------------------------

    def load_manifest(self, yaml_path: str) -> ExtensionManifest:
        """YAML ファイルからマニフェストを読み込み、レジストリに登録する。

        Args:
            yaml_path: manifest.yaml の絶対パスまたは相対パス。

        Returns:
            読み込まれた ExtensionManifest。

        Raises:
            RuntimeError: PyYAML が未インストールの場合。
            FileNotFoundError: ファイルが存在しない場合。
            ValueError: マニフェストに name が無い場合。
        """
        if not HAS_YAML:
            raise RuntimeError(
                "PyYAML が必要です。`pip install pyyaml` でインストールしてください。"
            )

        path = Path(yaml_path)
        if not path.is_file():
            raise FileNotFoundError(f"マニフェストが見つかりません: {yaml_path}")

        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        if "name" not in data:
            raise ValueError(f"マニフェストに 'name' が必要です: {yaml_path}")

        manifest = ExtensionManifest(
            name=data["name"],
            version=data.get("version", "0.1.0"),
            tier=data.get("tier", "tier2"),
            description=data.get("description", ""),
            entrypoint=data.get("entrypoint", ""),
            dependencies=data.get("dependencies", {"requires": [], "optional": []}),
            hooks=data.get("hooks", {"subscribes": [], "emits": []}),
            config=data.get("config", {"schema": "", "defaults": {}}),
            contracts=data.get("contracts", {"consumes": [], "produces": []}),
        )

        self.manifests[manifest.name] = manifest
        logger.info("マニフェスト登録: %s (%s)", manifest.name, manifest.version)
        return manifest

    # ------------------------------------------------------------------
    # 依存解決（トポロジカルソート）
    # ------------------------------------------------------------------

    def resolve(self) -> List[str]:
        """登録済みマニフェストを依存関係に基づきトポロジカルソートする。

        Returns:
            ロード順にソートされたエクステンション名のリスト。

        Raises:
            _CycleError: 循環依存が検出された場合。
            ValueError: 未登録の必須依存が見つかった場合。
        """
        # 依存グラフ構築
        graph: Dict[str, List[str]] = {}
        for name, manifest in self.manifests.items():
            requires = manifest.dependencies.get("requires", [])
            # 未登録の必須依存チェック
            for dep in requires:
                if dep not in self.manifests:
                    raise ValueError(
                        f"エクステンション '{name}' の必須依存 '{dep}' が未登録です"
                    )
            graph[name] = list(requires)

        return self._topological_sort(graph)

    @staticmethod
    def _topological_sort(graph: Dict[str, List[str]]) -> List[str]:
        """Kahn のアルゴリズムによるトポロジカルソート。

        Args:
            graph: name -> [依存先name] の隣接リスト。

        Returns:
            ソート済みリスト（依存先が先）。

        Raises:
            _CycleError: 循環依存が検出された場合。
        """
        # 入次数の計算
        in_degree: Dict[str, int] = {node: 0 for node in graph}
        for node, deps in graph.items():
            for dep in deps:
                if dep in in_degree:
                    in_degree[dep] = in_degree.get(dep, 0)

        # 各ノードへの入次数を再計算
        in_degree = {node: 0 for node in graph}
        for node, deps in graph.items():
            for dep in deps:
                if dep in in_degree:
                    pass  # dep は node の依存先 → node は dep の後
        # graph[node] = deps は「node は deps に依存」を意味する
        # つまり deps -> node の辺。dep の出次数ではなく node の入次数。
        in_degree = {node: 0 for node in graph}
        for node, deps in graph.items():
            # node は deps 各要素より後にロードされる必要がある
            # edges: dep -> node (dep が先)
            in_degree[node] += len(deps)

        # 入次数 0 のノードをキューへ（名前順で安定ソート）
        queue = sorted([n for n, d in in_degree.items() if d == 0])
        result: List[str] = []

        while queue:
            current = queue.pop(0)
            result.append(current)
            # current に依存している（current -> other の辺がある）ノードの入次数を減らす
            for node, deps in graph.items():
                if current in deps:
                    in_degree[node] -= 1
                    if in_degree[node] == 0:
                        # 挿入ソートで名前順を維持
                        inserted = False
                        for i, q in enumerate(queue):
                            if node < q:
                                queue.insert(i, node)
                                inserted = True
                                break
                        if not inserted:
                            queue.append(node)

        if len(result) != len(graph):
            remaining = set(graph.keys()) - set(result)
            raise _CycleError(
                f"循環依存が検出されました: {remaining}"
            )

        return result

    # ------------------------------------------------------------------
    # ロード
    # ------------------------------------------------------------------

    def load(
        self,
        event_bus: Any = None,
        config: Any = None,
    ) -> Dict[str, Extension]:
        """全登録エクステンションを依存順にロードする。

        Args:
            event_bus: EventBus インスタンス（エクステンションの context に渡される）。
            config: アプリケーション設定オブジェクト。

        Returns:
            name -> Extension インスタンスの辞書。
        """
        order = self.resolve()
        context = {
            "event_bus": event_bus,
            "config": config,
            "registry": self,
        }

        for name in order:
            manifest = self.manifests[name]
            if not manifest.entrypoint:
                logger.warning(
                    "エクステンション '%s' にエントリポイントが未定義です", name
                )
                continue

            try:
                ext_instance = self._instantiate(manifest.entrypoint)
                ext_instance.setup(context)
                self.extensions[name] = ext_instance
                logger.info("エクステンション ロード完了: %s", name)
            except Exception:
                logger.exception("エクステンション '%s' のロードに失敗しました", name)
                raise

        return self.extensions

    @staticmethod
    def _instantiate(entrypoint: str) -> Extension:
        """エントリポイント文字列からインスタンスを生成する。

        Args:
            entrypoint: "module.path:ClassName" 形式。

        Returns:
            Extension インスタンス。

        Raises:
            ValueError: フォーマットが不正な場合。
            ImportError: モジュールが見つからない場合。
            AttributeError: クラスが見つからない場合。
        """
        if ":" not in entrypoint:
            raise ValueError(
                f"エントリポイントは 'module:Class' 形式が必要です: {entrypoint}"
            )
        module_path, class_name = entrypoint.rsplit(":", 1)
        module = importlib.import_module(module_path)
        cls: Type[Extension] = getattr(module, class_name)
        return cls()

    # ------------------------------------------------------------------
    # 参照
    # ------------------------------------------------------------------

    def get_extension(self, name: str) -> Optional[Extension]:
        """ロード済みエクステンションを名前で取得する。

        Args:
            name: エクステンション名。

        Returns:
            Extension インスタンス。未ロードの場合は None。
        """
        return self.extensions.get(name)
