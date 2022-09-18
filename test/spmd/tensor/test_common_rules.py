# Copyright (c) Meta Platforms, Inc. and affiliates
import torch
from torch.testing._internal.common_utils import run_tests
from spmd.tensor.dispatch import OpSchema

from spmd.tensor.ops.common_rules import (
    einop_rule,
    reduction_rule,
    pointwise_rule,
)
from spmd.tensor.placement_types import DTensorSpec
from spmd.testing.common_utils import (  # type: ignore
    DistTensorTestBase,
    with_comms,
)
from spmd import DeviceMesh


class CommonRulesTest(DistTensorTestBase):
    @with_comms
    def test_einop_basic_propagation(self):
        # plain einsum, mm
        mesh = DeviceMesh(self.device_type, torch.arange(self.world_size))

        # propagate col-wise sharding
        mat1, mat2 = [-1, -1], [-1, 0]
        mat1_spec = DTensorSpec.from_dim_map(
            mesh, mat1, [], shape=torch.Size([8, 4])
        )
        mat2_spec = DTensorSpec.from_dim_map(
            mesh, mat2, [], shape=torch.Size([4, 8])
        )
        output_sharding = einop_rule(
            "mk,kn->mn", OpSchema((mat1_spec, mat2_spec), {})
        )
        output_spec = output_sharding.output_spec
        self.assertIsNotNone(output_spec)
        self.assertEqual(output_spec.dim_map, [-1, 0])
        self.assertEqual(output_spec.shape, torch.Size([8, 8]))

        # propagate row-wise sharding
        mat1, mat2 = [0, -1], [-1, -1]
        mat1_spec = DTensorSpec.from_dim_map(
            mesh, mat1, [], shape=torch.Size([8, 4])
        )
        mat2_spec = DTensorSpec.from_dim_map(
            mesh, mat2, [], shape=torch.Size([4, 8])
        )
        output_sharding = einop_rule(
            "mk,kn->mn", OpSchema((mat1_spec, mat2_spec), {})
        )
        output_spec = output_sharding.output_spec
        self.assertIsNotNone(output_spec)
        self.assertEqual(output_spec.dim_map, [0, -1])
        self.assertEqual(output_spec.shape, torch.Size([8, 8]))

        # generate partial
        mat1, mat2 = [-1, 0], [0, -1]
        mat1_spec = DTensorSpec.from_dim_map(
            mesh, mat1, [], shape=torch.Size([8, 4])
        )
        mat2_spec = DTensorSpec.from_dim_map(
            mesh, mat2, [], shape=torch.Size([4, 8])
        )
        output_sharding = einop_rule(
            "mk,kn->mn", OpSchema((mat1_spec, mat2_spec), {})
        )
        output_spec = output_sharding.output_spec
        self.assertIsNotNone(output_spec)
        self.assertTrue(output_spec.placements[0].is_partial())
        self.assertEqual(output_spec.shape, torch.Size([8, 8]))

    @with_comms
    def test_einop_pointwise_propagation(self):
        mesh = DeviceMesh(self.device_type, torch.arange(self.world_size))
        # addition
        mat1 = [0, -1]
        mat1_spec = DTensorSpec.from_dim_map(
            mesh, mat1, [], shape=torch.Size([8, 8])
        )
        output_sharding = einop_rule(
            "ij,ij->ij", OpSchema((mat1_spec, mat1_spec), {})
        )
        output_spec = output_sharding.output_spec
        self.assertIsNotNone(output_spec)
        self.assertEqual(output_spec.dim_map, [0, -1])
        self.assertEqual(output_spec.shape, torch.Size([8, 8]))

        # broadcast addition
        mat1 = [-1, 0, -1]
        mat1_spec = DTensorSpec.from_dim_map(
            mesh, mat1, [], shape=torch.Size([8, 4, 2])
        )
        mat2_spec = DTensorSpec.from_dim_map(
            mesh, [-1], [], shape=torch.Size([2])
        )
        output_sharding = einop_rule(
            "ijk,k->ijk", OpSchema((mat1_spec, mat2_spec), {})
        )
        output_spec = output_sharding.output_spec
        self.assertIsNotNone(output_spec)
        self.assertEqual(output_spec.dim_map, [-1, 0, -1])
        self.assertEqual(output_spec.shape, torch.Size([8, 4, 2]))

        # broadcast to a common shape
        mat1_spec = DTensorSpec.from_dim_map(
            mesh, [0, -1, -1], [], shape=torch.Size([8, 8, 8])
        )
        mat2_spec = DTensorSpec.from_dim_map(
            mesh, [-1, -1], [], shape=torch.Size([1, 8])
        )
        output_sharding = einop_rule(
            "ijk,1k->ijk", OpSchema((mat1_spec, mat2_spec), {})
        )
        output_spec = output_sharding.output_spec
        self.assertIsNotNone(output_spec)
        self.assertEqual(output_spec.dim_map, [0, -1, -1])
        self.assertEqual(output_spec.shape, torch.Size([8, 8, 8]))

    @with_comms
    def test_einop_merge_sharding(self):
        # 2d mesh einop merge sharding
        mesh_shape = torch.arange(self.world_size).reshape(
            self.world_size // 2, self.world_size // 2
        )
        mesh = DeviceMesh(self.device_type, mesh_shape)
        mat1, mat2 = [0, -1], [-1, 1]
        mat1_spec = DTensorSpec.from_dim_map(
            mesh, mat1, [], shape=torch.Size([8, 4])
        )
        mat2_spec = DTensorSpec.from_dim_map(
            mesh, mat2, [], shape=torch.Size([4, 8])
        )
        output_sharding = einop_rule(
            "mk,kn->mn", OpSchema((mat1_spec, mat2_spec), {})
        )
        output_spec = output_sharding.output_spec
        self.assertIsNotNone(output_spec)
        self.assertEqual(output_spec.dim_map, [0, 1])
        self.assertEqual(output_spec.shape, torch.Size([8, 8]))

    @with_comms
    def test_einop_linearity(self):
        mesh_shape = torch.arange(self.world_size).reshape(
            self.world_size // 2, self.world_size // 2
        )
        mesh = DeviceMesh(self.device_type, mesh_shape)

        mat1, mat2 = [0, -1], [-1, -1]
        mat1_spec = DTensorSpec.from_dim_map(
            mesh, mat1, [1], shape=torch.Size([8, 4])
        )
        mat2_spec = DTensorSpec.from_dim_map(
            mesh, mat2, [], shape=torch.Size([4, 8])
        )
        # if not turn on linearity, partial sum is not eligible to propagate, we return
        # suggestion to reshard inputs with no partial sum (i.e. all_reduce one input)
        output_sharding = einop_rule(
            "mk,kn->mn", OpSchema((mat1_spec, mat2_spec), {})
        )
        self.assertIsNone(output_sharding.output_spec)
        suggestions = output_sharding.schema_suggestions
        self.assertIsNotNone(suggestions)
        suggested_spec = suggestions[0].args_schema[0]
        self.assertFalse(suggested_spec.placements[1].is_partial())

        # einop prop with linearity on mm, should give back suggestion
        # on converting placements to partial
        output_sharding = einop_rule(
            "mk,kn->mn", OpSchema((mat1_spec, mat2_spec), {}), linearity=True
        )
        self.assertIsNone(output_sharding.output_spec)
        suggestions = output_sharding.schema_suggestions
        self.assertIsNotNone(suggestions)
        mat2_spec = suggestions[0].args_schema[1]
        # mat2 mesh dim 1 should become partial now!
        self.assertTrue(mat2_spec.placements[1].is_partial())

        # einop prop with linearity on point-wise, should give back suggestion
        # on converting placements to partial
        mat1, mat2 = [0, -1], [0, -1]
        mat1_spec = DTensorSpec.from_dim_map(
            mesh, mat1, [1], shape=torch.Size([8, 6])
        )
        mat2_spec = DTensorSpec.from_dim_map(
            mesh, mat2, [], shape=torch.Size([8, 6])
        )

        output_sharding = einop_rule(
            "ij,ij->ij", OpSchema((mat1_spec, mat2_spec), {}), linearity=True
        )
        self.assertIsNone(output_sharding.output_spec)
        suggestions = output_sharding.schema_suggestions
        self.assertIsNotNone(suggestions)
        mat2_spec = suggestions[0].args_schema[1]
        # mat2 mesh dim 1 should become partial now!
        self.assertTrue(mat2_spec.placements[1].is_partial())

    @with_comms
    def test_einop_errors(self):
        mesh_shape = torch.arange(self.world_size).reshape(
            self.world_size // 2, self.world_size // 2
        )
        mesh = DeviceMesh(self.device_type, mesh_shape)

        mat1, mat2 = [0, -1], [0, 1]
        mat1_spec = DTensorSpec.from_dim_map(
            mesh, mat1, [], shape=torch.Size([8, 4])
        )
        mat2_spec = DTensorSpec.from_dim_map(
            mesh, mat2, [], shape=torch.Size([8, 4])
        )
        with self.assertRaisesRegex(RuntimeError, "across the same mesh dim!"):
            einop_rule("mk,kn->mn", OpSchema((mat1_spec, mat2_spec), {}))

        mat1, mat2 = [0, -1], [1, -1]
        mat1_spec = DTensorSpec.from_dim_map(
            mesh, mat1, [], shape=torch.Size([8, 4])
        )
        mat2_spec = DTensorSpec.from_dim_map(
            mesh, mat2, [], shape=torch.Size([8, 4])
        )

        with self.assertRaisesRegex(
            RuntimeError, "sharded two different ways:"
        ):
            einop_rule("ij,ij->ij", OpSchema((mat1_spec, mat2_spec), {}))

    @with_comms
    def test_pointwise_rules_suggestion(self):
        mesh = DeviceMesh(self.device_type, torch.arange(self.world_size))

        # propagate point-wise sharding
        inp1, inp2 = [-1, -1], [-1, 0]
        mat1_spec = DTensorSpec.from_dim_map(
            mesh, inp1, [], shape=torch.Size([8, 4])
        )
        mat2_spec = DTensorSpec.from_dim_map(
            mesh, inp2, [], shape=torch.Size([8, 4])
        )
        # adding a positional argument -1 to arg schema
        output_sharding = pointwise_rule(
            OpSchema((mat1_spec, mat2_spec, -1), {})
        )
        self.assertIsNone(output_sharding.output_spec)
        self.assertIsNotNone(output_sharding.schema_suggestions)

        # ensure that the suggestion from pointwise rules still have
        # the positional args that are not DTensorSpec
        schema_suggestion = output_sharding.schema_suggestions[0]
        self.assertEqual(len(schema_suggestion.args_schema), 3)
        self.assertEqual(schema_suggestion.args_schema[2], -1)

    @with_comms
    def test_reduction_rule(self):
        mesh = DeviceMesh(self.device_type, torch.arange(self.world_size))
        # reduction on a 2d mat
        mat1 = [0, -1]
        mat1_spec = DTensorSpec.from_dim_map(
            mesh, mat1, [], shape=torch.Size([8, 4])
        )
        # reduction on dim 0
        output_sharding_0 = reduction_rule(OpSchema((mat1_spec, 0), {}))
        self.assertIsNotNone(output_sharding_0.output_spec)
        self.assertEqual(output_sharding_0.output_spec.dim_map, [-1])
        # pending sum on dim 0
        self.assertEqual(output_sharding_0.output_spec.sums, [0])
        self.assertEqual(output_sharding_0.output_spec.shape, torch.Size([4]))

        # reduction on dim 1
        output_sharding_1 = reduction_rule(OpSchema((mat1_spec, 1), {}))
        self.assertIsNotNone(output_sharding_1.output_spec)
        self.assertEqual(output_sharding_1.output_spec.dim_map, [0])
        self.assertEqual(output_sharding_1.output_spec.sums, [])
        self.assertEqual(output_sharding_1.output_spec.shape, torch.Size([8]))

        # full reduction if not specify dim
        output_sharding_all_dim = reduction_rule(OpSchema((mat1_spec,), {}))
        self.assertIsNotNone(output_sharding_all_dim.output_spec)
        self.assertEqual(output_sharding_all_dim.output_spec.dim_map, [])
        # pending sum on mesh
        self.assertEqual(output_sharding_all_dim.output_spec.sums, [0])
        self.assertEqual(
            output_sharding_all_dim.output_spec.shape, torch.Size([])
        )


if __name__ == "__main__":
    run_tests()