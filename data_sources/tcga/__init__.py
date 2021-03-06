from collections import defaultdict, UserList
from contextlib import contextmanager
from glob import glob
from statistics import StatisticsError
from tarfile import TarFile
from typing import Union
from warnings import warn

import numpy
from pandas import concat, read_table, Series
from rpy2.rinterface import RRuntimeError

from config import DATA_DIR
from data_sources.data_source import DataSource

from metrics import signal_to_noise, signal_to_noise_vectorized
from models import ExpressionProfile
from helpers.r import importr, r2p

from multi_view.layers import MutationLayer, ExpressionLayer
from multi_view.layers import Layer
from layers_data import LayerData, LayerDataWithSubsets, Subset, MutationAnnotations

from .barcode import TCGABarcode


def download_with_firebrowser(method, page_size=2000, **kwargs):
        pages = []
        finished = False
        page_nr = 1
        first_page = None
        while not finished: 
            page = method(format='csv', page=page_nr, page_size=page_size, **kwargs)
            print(page_nr)
            page = r2p(page)
            if first_page is None:
                first_page = page
            else:
                page.columns = first_page.columns

            page_nr += 1
            if len(page) < page_size:
                finished = True
            pages.append(page)
        return concat(pages)


class TCGAMutationAnnotations(MutationAnnotations):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def _ensure_participant(self):
        if 'participant' not in self.columns:
            self['participant'] = self.tumor_sample_barcode.apply(
                lambda barcode: TCGABarcode(barcode).participant
            )

    def _set_variant_id(self):
        self['variant_id'] = self.apply(
            lambda m: f'{m.chromosome}_{m.start_position}_{m.reference_allele}_{m.tumor_seq_allele2}_b{m.ncbi_build}',
            axis='columns'
        )

    def as_layer(self, *args, **kwargs):
        self._ensure_participant()
        return super().as_layer(*args, **kwargs)


class ExpressionManager(LayerDataWithSubsets, ExpressionProfile):

    def __init__(self, *args, name='expression data', layer_type=ExpressionLayer, **kwargs):
        super().__init__(*args, **kwargs)
        self.name = name
        self.layer_type = layer_type

    def barcodes_for_type(self, sample_type):
        assert sample_type in TCGABarcode.sample_type_ranges.values()
        return [
            barcode
            for barcode in self.barcodes
            if barcode.sample_type == sample_type
        ]

    def limit_to_sample_type(self, sample_type):
        chosen_columns = self.barcodes_for_type(sample_type)
        return self[[barcode.barcode for barcode in chosen_columns]]

    __type_subsets__ = {
        sample_type: Subset(
            filter=(lambda sample_type: lambda expression: expression.limit_to_sample_type(sample_type))(sample_type),
            type=ExpressionLayer
        )
        for sample_type in TCGABarcode.sample_type_ranges.values()
    }
    __subsets__ = {
        **{
        },
        **__type_subsets__
    }

    @property
    def classes(self):
        return Series([barcode.sample_type for barcode in self.barcodes])

    class ParticipantSamples(UserList):

        @property
        def by_type(self):
            return {
                sample_type: {
                    barcode
                    for barcode in self
                    if barcode.sample_type == sample_type
                }
                for sample_type in TCGABarcode.sample_type_ranges.values()
            }

    @property
    def barcodes(self):
        # TODO: recently changed test me
        return Series([
            TCGABarcode(column_name)
            for column_name in self.columns
        ])

    def samples_by_participant(self):
        by_participant = defaultdict(self.ParticipantSamples)
        for barcode in self.barcodes:
            by_participant[barcode.participant].append(barcode)
        return by_participant

    def paired(self, type_one, type_two, limit_to=None, spread=None):
        """
        GSEADesktop requires equal number of cases and controls; to comply with this requirement,
        one can limit number of samples per participant (e.g. one control and one case) or
        spread the controls (e.g. if we have two cases and one control, we duplicate the control)
        """
        assert not (spread and limit_to)
        paired = []
        for participant_samples in self.samples_by_participant().values():
            samples_by_type = participant_samples.by_type
            if samples_by_type[type_one] and samples_by_type[type_two]:
                if limit_to:
                    for type in [type_one, type_two]:
                        paired.extend(list(samples_by_type[type])[:limit_to])
                elif spread:
                    types = [type_one, type_two]
                    assert spread in types
                    types.remove(spread)
                    print(types)
                    assert len(types) == 1
                    not_spread = types[0]
                    cases = samples_by_type[not_spread]
                    controls = samples_by_type[spread]
                    assert len(controls) == 1
                    paired.extend(cases)
                    for i in range(len(cases)):
                        paired.extend(controls)
                else:
                    paired.extend(participant_samples)

        expressions_paired = self[[barcode.barcode for barcode in paired]]
        return expressions_paired

    def by_sample_type(self, type_name, paired_against=None):
        if paired_against:
            subset = self.paired(type_name, paired_against)
        else:
            subset = self
        return subset[subset.columns[subset.classes == type_name]]

    def split(self, case_='tumor', control_='normal', only_paired=True):
        if only_paired:
            paired = self.paired(case_, control_)
        else:
            paired = self
        print(f'Using {len(paired.columns)} out of {len(self.columns)} samples')

        if paired.empty:
            return

        cases = paired[paired.columns[paired.classes == case_]]
        controls = paired[paired.columns[paired.classes == control_]]

        return cases, controls

    def differential(self, case_='tumor', control_='normal', metric=signal_to_noise, index_as_bytes=True,
                     limit_to=None, only_paired=True, nans='fill_0', additional_controls=None):
        print(f'Metric: {metric.__name__}, groups: {case_}, {control_}')

        case, control = self.split(case_, control_, only_paired)

        if additional_controls is not None:
            if len(control.columns.difference(additional_controls.columns)):
                # there are some controls in "control" that are not in additional columns
                # (additional columns are not a superset, though may overlap)
                control = concat([control, additional_controls], axis=1).T.drop_duplicates().T
            else:
                control = additional_controls
        diff = []

        if case.empty or control.empty:
            warn('Case or control is empty')
            return

        genes = set(case.index)
        if limit_to:
            genes = genes & set(limit_to)
        genes = list(genes)
        try:
            if metric is signal_to_noise:
                query_signature = signal_to_noise_vectorized(case.loc[genes], control.loc[genes])
            else:
                for gene in genes:
                    diff.append(metric(case.loc[gene], control.loc[gene]))
                query_signature = Series(diff, index=genes)
        except StatisticsError:
            warn(f'Couldn\'t compute metric: {metric} for {case} and {control}')
            return
        if nans == 'fill_0':
            query_signature = query_signature.fillna(0)
        if index_as_bytes:
            query_signature.index = query_signature.index.astype(bytes)
        return query_signature


class TCGAExpression(DataSource):

    path_template = '{self.path}/gdac.broadinstitute.org_{cancer_type}.Merge_rnaseqv2__illuminahiseq_rnaseqv2__unc_edu__Level_3__RSEM_genes_normalized__data.Level_3.2016012800.0.0.tar.gz'

    file_in_tar = (
        'gdac.broadinstitute.org_{cancer_type}.'
        'Merge_rnaseqv2__illuminahiseq_rnaseqv2'
        '__unc_edu__Level_3__RSEM_genes_normalized__data.'
        'Level_3.2016012800.0.0/'
        '{cancer_type}.rnaseqv2__illuminahiseq_rnaseqv2'
        '__unc_edu__Level_3__RSEM_genes_normalized__data.data.txt'
    )

    id_type = 'gene_id'
    read_type = 'normalized_count'

    def __init__(self, path):
        self.path = path

    @contextmanager
    def _get_expression_file(self, cancer_type):

        path = self.path_template.format(self=self, cancer_type=cancer_type)

        with TarFile.open(path) as tar:
            member = tar.getmember(
                self.file_in_tar.format(cancer_type=cancer_type)
            )
            yield tar.extractfile(member)

    def data(self, cancer_type, index='entrez_gene_id', read_type=None) -> Layer:

        read_type = read_type or self.read_type

        with self._get_expression_file(cancer_type) as f:
            # verify the first row which specifies the type of measurements
            mrna = read_table(f, index_col=0, nrows=1)

            cols = (mrna.loc[self.id_type] == read_type)
            types = set(mrna.loc[self.id_type])
            if types != {read_type}:
                print(f'Choosing {read_type} out of {types}')
            assert set(types) & {read_type}

            f.seek(0)

            mrna = read_table(
                f,
                index_col=0,
                usecols=[0] + [i + 1 for i, v in enumerate(cols) if v] if any(cols) else None,
                skiprows=[1]  # skip the measurements row as this is the only non-numeric row which prevents pandas from
                # correct casting of the value to float64
            )

            if types != {read_type}:
                mrna.columns = [
                    c[:-2] for c in mrna.columns
                ]

        if index != 'Hybridization REF':
            mrna['hugo_symbol'], mrna['entrez_gene_id'] = mrna.index.str.split('|', 1).str

            possible_indices = ['hugo_symbol', 'entrez_gene_id']
            assert index in possible_indices

            possible_indices.remove(index)
            mrna = mrna.drop(columns=possible_indices, axis=1)
            mrna = mrna.reset_index(drop=True).set_index(index)

            for column in mrna.columns:
                try:
                    assert mrna[column].dtype == numpy.float64
                except AssertionError:
                    print(column)

        return ExpressionManager(mrna, name=f'{cancer_type} expression')

    def genes(self, cancer_type, index='entrez_gene_id') -> Series:

        with self._get_expression_file(cancer_type) as f:

            mrna_index = read_table(f, usecols=[0], skiprows=[1], index_col=0)

        genes = {}

        if index != 'Hybridization REF':
            mrna_index.index.name = index
            genes['hugo_symbol'], genes['entrez_gene_id'] = mrna_index.index.str.split('|', 1).str
            return genes[index]
        else:
            return mrna_index.index

    def cohorts(self):
        """Returns cohorts with downloaded expression data"""
        glob_path = self.path_template.format(self=self, cancer_type='*')
        prefix_len, suffix_len = map(len, glob_path.split('*'))
        return [
            path[prefix_len:-suffix_len]
            for path in glob(glob_path)
        ]

    
class miRNAExpression(TCGAExpression):
    
    path_template = '{self.path}//gdac.broadinstitute.org_{cancer_type}.Merge_mirnaseq__illuminahiseq_mirnaseq__bcgsc_ca__Level_3__miR_gene_expression__data.Level_3.2016012800.0.0.tar.gz'
    
    file_in_tar = (
        'gdac.broadinstitute.org_{cancer_type}.Merge_mirnaseq__illuminahiseq_mirnaseq__bcgsc_ca__Level_3__miR_gene_expression__data.Level_3.2016012800.0.0/{cancer_type}.mirnaseq__illuminahiseq_mirnaseq__bcgsc_ca__Level_3__miR_gene_expression__data.data.txt'
    )
    id_type = 'miRNA_ID'
    read_type = 'reads_per_million_miRNA_mapped'
    
    def genes(self, cancer_type, index='Hybridization REF'):
        return super().genes(cancer_type, index)
    
    def data(self, cancer_type, index='Hybridization REF', read_type=read_type):
        return super().data(cancer_type, index)
    

class TCGA(DataSource):

    # 'clinical', 'rnaseq', 'mutations', 'RPPA', 'mRNA', 'miRNASeq', 'methylation', 'isoforms'

    path = DATA_DIR + '/tcga'

    def add_participant_column(self, df: Union[Layer, LayerData], column: str= 'tumor_sample_barcode'):
        if 'participant' not in df.columns:
            df['participant'] = [
                TCGABarcode(barcode).participant
                for barcode in getattr(df, column)
            ]
        return df

    @property
    def expression(self) -> TCGAExpression:
        return TCGAExpression(self.path)
    
    @property
    def mirna_expression(self) -> miRNAExpression:
        return miRNAExpression(self.path)

    def clinical(self, cancer_type, mode='simple') -> Layer:
        clinical_data = read_table(
            f'{DATA_DIR}/tcga/gdac.broadinstitute.org_'
            f'{cancer_type}.Clinical_Pick_Tier1.Level_4.2016012800.0.0/'
            + (
                f'{cancer_type}.clin.merged.picked.txt'
                if mode == 'simple'
                else 'All_CDEs.txt'
            ),
            index_col=[0]
        )
        clinical_data.columns = clinical_data.columns.str.upper()
        clinical_data = clinical_data.T
        clinical_data = self.add_participant_column(clinical_data, column='index')
        return Layer(clinical_data)

    def mutations(self, cancer_type, participants=None, barcodes=None):
        paths = self._locate_files(
            cancer_type,
            data_type='Mutation_Packager_Calls',
            file_type='maf.txt',
            level=3,
            limit_to=participants
        )

        if barcodes is not None:
            barcodes = [
                TCGABarcode(barcode).up_to_sample_type for barcode in barcodes
            ]
        else:

            if participants:
                participants = set(participants)
                barcodes = []
                for barcode in paths.keys():
                    if barcode.participant in participants:
                        barcodes.append(barcode)
                        participants.remove(barcode.participant)

                if participants:
                    warn(f'No mutations for {participants}')
            else:
                barcodes = list(paths.keys())

        dfs = []
        for barcode in barcodes:
            try:
                df = read_table(paths[barcode])
                dfs.append(df)
            except KeyError:
                warn(f'No mutations for {barcode}')
        df = concat(dfs).reset_index(drop=True)
        return TCGAMutationAnnotations(df)
    
    supported_layers = {
        # layer class: default layer generator method
        MutationLayer: mutations
    }

    def _locate_files(self, cancer_type, data_type, file_type, level, limit_to=None):
        from glob import glob
        from pathlib import Path
        paths_by_participant = {}
        for path in glob(f'{self.path}/gdac.broadinstitute.org_{cancer_type}.{data_type}.Level_{level}.2016012800.0.0/TCGA-*-*-01.{file_type}'):
            path = Path(path)
            # assert path.name.endswith('.' + file_type)
            aliquot = TCGABarcode(path.name[:-8])
            participant = aliquot.participant
            if participant in paths_by_participant:
                raise ValueError('More than one sample per participant')
            if limit_to and participant not in limit_to:
                continue
            paths_by_participant[aliquot] = path
        return paths_by_participant
    
    def significantly_mutated_genes(self):
        # Significantly mutated genes, als worth investigating:
        # x = firebrowse_r.Analyses_Mutation_SMG(format='csv', cohort='BRCA')
        pass
    
    def significant_mutations(self, tool_name, **kwargs) -> TCGAMutationAnnotations:
        tool = self.prioritization_tools[tool_name]
        raw_data = tool(**kwargs)

        return TCGAMutationAnnotations(raw_data)

    def _mut_sigv_two_cv_mutations(self, **kwargs):
        """Uses FirebrowseR to fetch significant mutations as called by MutSig2CV"""
        try:
            firebrowse_r = importr('FirebrowseR')
        except RRuntimeError:
            warn('No firebrowse_r found')
        method = firebrowse_r.Analyses_Mutation_MAF
        return download_with_firebrowser(method)
    
    prioritization_tools = {
        'MutSig2CV': _mut_sigv_two_cv_mutations
    }
