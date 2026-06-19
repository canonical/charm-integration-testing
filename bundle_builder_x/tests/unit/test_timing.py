# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging

from bundle_builder_x.timing import Timeline


class TestTimeline:
    def test_child_reuses_timeline_logger(self) -> None:
        # GIVEN a timeline with a base logger
        logger = logging.getLogger("bundle_builder_x")
        timeline = Timeline(logger=logger)

        # WHEN creating nested child timelines
        child = timeline.child("solve")
        grandchild = child.child("phase")

        # THEN the logger name stays stable instead of appending .timeline repeatedly
        assert timeline.logger is not None
        assert child.logger is not None
        assert grandchild.logger is not None
        assert timeline.logger.name == "bundle_builder_x.timeline"
        assert child.logger.name == "bundle_builder_x.timeline"
        assert grandchild.logger.name == "bundle_builder_x.timeline"
