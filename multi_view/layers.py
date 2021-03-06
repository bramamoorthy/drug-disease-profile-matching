from typing import Mapping, List

from pandas import DataFrame, Series, merge
from sklearn.preprocessing import minmax_scale
from numpy import nan
from statistics import mean
from copy import deepcopy

from bio.protein_sequences import ProteinSequences
from config import DATA_DIR
from data_frames import AugmentedDataFrame
from functools import reduce


class Layers:

    def __init__(self, data: Mapping[str, DataFrame], mapper=lambda x: x, filter=lambda x: True):
        self.layers = {}
        for k, v in data.items():
            self.layers[k] = deepcopy(v)
        self.filter(filter)
        for k, v in data.items():
            v.index = v.index.map(mapper)
            self.layers[k] = v

    def filter(self, func):
        self.layers = {
            k: v[v.index.map(func)] for k, v in self.layers.items()
        }
        return self

    def to_df(self) -> DataFrame:
        if len(self.layers) < 2:
            return list(self.layers.values())[0]
        return reduce(
            lambda left, right: merge(
                left, right,
                left_on=left.index,
                right_on=right.index
            ),
            self.layers.values()
        ).set_index('key_0')

    def rescale(self, scaler=minmax_scale) -> 'Layers':
        scaled = {}

        for key, value in self.layers.items():
            scaled[key] = DataFrame(
                index=value.index,
                columns=value.columns,
                data=scaler(value)
            )

        return Layers(scaled)

    @property
    def layers_with_negatives(self) -> List[str]:
        return [
            key
            for key, value in self.layers.items()
            if value.min().min() < 0
        ]

    def scale_proportionally_to_certainty(self, scaler=None):
        scaled = {}

        if scaler:
            self = self.rescale(scaler=scaler)

        from multi_view.nmf import MultiViewNMF

        # TODO: I use only single view features here, maybe another class for that?
        nmf = MultiViewNMF()

        flip = True if self.layers_with_negatives else False

        for key, value in self.layers.items():
            # one could do supervised learning here, optimizing a scaling factor for each layer
            # or it could be related to the internal performance of clustering of individual layers
            # (if a layer does not know how to cluster into three parts, it is not very useful in this case)

            # if rescaled_layers.layers_with_negatives:

            n = nmf.flip_negatives(value) if flip else nmf.set_matrix(value)

            W, H = n.decompose()
            p = W.predictions_certainty_combined()
            w = mean(p.fillna(0))

            # here is an important observation: the more certain the layer,
            # the worse is the clustering. Why? Possibly there is a confounding
            # factor of the data types provided for each of the layers:
            # mutations are very sparse and single occurrences can lead to both:
            # incorrect clustering classification AND high certainty (if it is the only mutation)

            # maybe is is a wrong approach? maybe it has to to applied only for weighted,
            # combination of data from single layer clustering???? 

            # anyway, I could look into strategies for imputing/downgrading spurious mutations
            scaled[key] = value * w  # uniform(0, 1)

        return Layers(scaled)

    def __repr__(self):
        r = ', '.join(
            [f'{k}: {v.shape}' for k, v in self.layers.items()]
        )
        return f'<Layers: {r}>'


class Layer(AugmentedDataFrame):
    """
    rows = samples, patients
    columns = genes, features
    """

    def limit_to_samples(self, samples_set):
        return self[self.index.isin(samples_set)]

    def rearrange_to_match_with(self, other_layer, how='left', match='rows'):
        assert match == 'rows'
        index_only = other_layer[[]]
        rearranged = merge(
            index_only, self,
            left_on=index_only.index, right_on=self.index,
            how=how
        ).set_index('key_0')
        return self._constructor(rearranged)

    def preprocess(self, rearrange_against=None):
        new_layer = (
            self.rearrange_to_match_with(rearrange_against)
            if rearrange_against is not None else
            self
        )
        return (
            new_layer
            .drop_useless_features()
            .clean_index_names()
            .fillna(0)
        )

    def clean_index_names(self):
        self.columns.name = ''
        self.index.name = ''
        return self

    def drop_useless_features(self):
        without_na = self.dropna(axis='columns', how='all')
        # print()
        # print(without_na)
        unique_count = without_na.apply(Series.nunique)
        # print(unique_count)
        without_variance = unique_count[unique_count == 1].index
        # print(without_variance)
        return without_na.drop(without_variance, axis='columns')


class MutationLayer(Layer):
    __default_normalizer__ = None

    # @cached_property
    @property
    def normalized(self):
        return self.__default_normalizer__()


class ExpressionLayer(Layer):
    pass


protein_sequences = None


# if not hierarchy I can always refactor to mixins for more flexibility
class CodingMutationLayer(MutationLayer):

    def __init__(self, sequences_path=DATA_DIR + '/uniprot/uniprot_sprot.fasta'):
        global protein_sequences
        if not protein_sequences:
            protein_sequences = ProteinSequences(sequences_path)

    def normalize_by_protein_length(self, recover=True):

        from statistics import StatisticsError
        failed = 0

        def normalize(column):
            try:
                protein = column.name
                protein_length = protein_sequences.average_length(protein)
                return column / protein_length
            except (StatisticsError, KeyError):
                nonlocal failed
                failed += 1
                if recover:
                    return column / protein_sequences.average_length()
                else:
                    return column * nan

        if failed:
            from warnings import warn
            warn(f'For {failed} proteins the exact lengths were not available; average protein length was used instead')

        return self.apply(normalize, axis='rows')

    __default_normalizer__ = normalize_by_protein_length


class NonCodingMutationLayer(MutationLayer):

    def normalize_by_gene_length(self):
        pass
