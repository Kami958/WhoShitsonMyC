"""core/wingeom 纯几何函数测试：宽度夹取、存储换算往返、缓动曲线。"""

from core import wingeom


class TestClampPanelWidth:
    def test_within_range_unchanged(self):
        assert wingeom.clamp_panel_width(340) == 340
        assert wingeom.clamp_panel_width(240) == 240
        assert wingeom.clamp_panel_width(640) == 640

    def test_out_of_range_clamped(self):
        assert wingeom.clamp_panel_width(100) == wingeom.PANEL_W_MIN
        assert wingeom.clamp_panel_width(99999) == wingeom.PANEL_W_MAX

    def test_invalid_falls_back_to_default(self):
        assert wingeom.clamp_panel_width(None) == wingeom.PANEL_W_DEFAULT
        assert wingeom.clamp_panel_width("abc") == wingeom.PANEL_W_DEFAULT

    def test_float_rounds(self):
        assert wingeom.clamp_panel_width(339.6) == 340


class TestStoredRoundtrip:
    def test_open_state_identity(self):
        # 展开态：存储宽度就是实际宽度
        assert wingeom.to_stored_width(
            1200, panel_open=True, panel_width=340
        ) == 1200
        assert wingeom.to_launch_width(
            1200, panel_open=True, panel_width=340
        ) == 1200

    def test_closed_state_roundtrip(self):
        # 收起态：存储 = 实际 + 侧栏宽，换算回去必须还原
        actual = 1200
        stored = wingeom.to_stored_width(
            actual, panel_open=False, panel_width=340
        )
        assert stored == actual + 340
        assert (
            wingeom.to_launch_width(stored, panel_open=False, panel_width=340)
            == actual
        )

    def test_panel_width_normalized_in_conversion(self):
        # 侧栏宽度越界时按夹取后的值换算，保证与存储一致
        stored = wingeom.to_stored_width(
            1200, panel_open=False, panel_width=50
        )
        assert stored == 1200 + wingeom.PANEL_W_MIN

    def test_repeated_toggle_does_not_drift(self):
        # 开合往返多次，存储宽度不漂移
        stored = 1400
        for _ in range(5):
            actual = wingeom.to_launch_width(
                stored, panel_open=False, panel_width=340
            )
            stored = wingeom.to_stored_width(
                actual, panel_open=False, panel_width=340
            )
        assert stored == 1400


class TestEaseProgress:
    def test_module_has_no_animation_symbols(self):
        # 侧栏开合已改为窗口一次性 resize，动画函数随之下架
        assert not hasattr(wingeom, "ease_progress")
        assert not hasattr(wingeom, "interpolate_width")
        assert not hasattr(wingeom, "PANEL_ANIM_MS")
