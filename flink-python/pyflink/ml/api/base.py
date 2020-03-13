################################################################################
#  Licensed to the Apache Software Foundation (ASF) under one
#  or more contributor license agreements.  See the NOTICE file
#  distributed with this work for additional information
#  regarding copyright ownership.  The ASF licenses this file
#  to you under the Apache License, Version 2.0 (the
#  "License"); you may not use this file except in compliance
#  with the License.  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
# limitations under the License.
################################################################################

import re

from abc import ABCMeta, abstractmethod

from pyflink.table.table_environment import TableEnvironment
from pyflink.table.table import Table
from pyflink.ml.api.param import WithParams, Params
from py4j.java_gateway import get_field


class PipelineStage(WithParams):
    """
    Base class for a stage in a pipeline. The interface is only a concept, and does not have any
    actual functionality. Its subclasses must be either Estimator or Transformer. No other classes
    should inherit this interface directly.

    Each pipeline stage is with parameters, and requires a public empty constructor for
    restoration in Pipeline.
    """

    def __init__(self, params=None):
        if params is None:
            self._params = Params()
        else:
            self._params = params

    def get_params(self) -> Params:
        return self._params

    def _convert_params_to_java(self, j_pipeline_stage):
        for param in self._params._param_map:
            java_param = self._make_java_param(j_pipeline_stage, param)
            java_value = self._make_java_value(self._params._param_map[param])
            j_pipeline_stage.set(java_param, java_value)

    @staticmethod
    def _make_java_param(j_pipeline_stage, param):
        # camel case to snake case
        name = re.sub(r'(?<!^)(?=[A-Z])', '_', param.name).upper()
        return get_field(j_pipeline_stage, name)

    @staticmethod
    def _make_java_value(obj):
        """ Convert Python object into Java """
        if isinstance(obj, list):
            obj = [PipelineStage._make_java_value(x) for x in obj]
        return obj

    def to_json(self) -> str:
        return self.get_params().to_json()

    def load_json(self, json: str) -> None:
        self.get_params().load_json(json)


class Transformer(PipelineStage):
    """
    A transformer is a PipelineStage that transforms an input Table to a result Table.
    """

    __metaclass__ = ABCMeta

    @abstractmethod
    def transform(self, table_env: TableEnvironment, table: Table) -> Table:
        """
        Applies the transformer on the input table, and returns the result table.

        :param table_env: the table environment to which the input table is bound.
        :param table: the table to be transformed
        :returns: the transformed table
        """
        raise NotImplementedError()


class JavaTransformer(Transformer):
    """
    Base class for Transformer that wrap Java implementations. Subclasses should
    ensure they have the transformer Java object available as j_obj.
    """

    def __init__(self, j_obj):
        super().__init__()
        self._j_obj = j_obj

    def transform(self, table_env: TableEnvironment, table: Table) -> Table:
        """
        Applies the transformer on the input table, and returns the result table.

        :param table_env: the table environment to which the input table is bound.
        :param table: the table to be transformed
        :returns: the transformed table
        """
        self._convert_params_to_java(self._j_obj)
        return Table(self._j_obj.transform(table_env._j_tenv, table._j_table))


class Model(Transformer):
    """
    Abstract class for models that are fitted by estimators.

    A model is an ordinary Transformer except how it is created. While ordinary transformers
    are defined by specifying the parameters directly, a model is usually generated by an Estimator
    when Estimator.fit(table_env, table) is invoked.
    """

    __metaclass__ = ABCMeta


class JavaModel(JavaTransformer, Model):
    """
    Base class for JavaTransformer that wrap Java implementations.
    Subclasses should ensure they have the model Java object available as j_obj.
    """


class Estimator(PipelineStage):
    """
    Estimators are PipelineStages responsible for training and generating machine learning models.

    The implementations are expected to take an input table as training samples and generate a
    Model which fits these samples.
    """

    __metaclass__ = ABCMeta

    def fit(self, table_env: TableEnvironment, table: Table) -> Model:
        """
        Train and produce a Model which fits the records in the given Table.

        :param table_env: the table environment to which the input table is bound.
        :param table: the table with records to train the Model.
        :returns: a model trained to fit on the given Table.
        """
        raise NotImplementedError()


class JavaEstimator(Estimator):
    """
    Base class for Estimator that wrap Java implementations.
    Subclasses should ensure they have the estimator Java object available as j_obj.
    """

    def __init__(self, j_obj):
        super().__init__()
        self._j_obj = j_obj

    def fit(self, table_env: TableEnvironment, table: Table) -> JavaModel:
        """
        Train and produce a Model which fits the records in the given Table.

        :param table_env: the table environment to which the input table is bound.
        :param table: the table with records to train the Model.
        :returns: a model trained to fit on the given Table.
        """
        self._convert_params_to_java(self._j_obj)
        return JavaModel(self._j_obj.fit(table_env._j_tenv, table._j_table))


class Pipeline(Estimator, Model, Transformer):
    """
    A pipeline is a linear workflow which chains Estimators and Transformers to
    execute an algorithm.

    A pipeline itself can either act as an Estimator or a Transformer, depending on the stages it
    includes. More specifically:


    If a Pipeline has an Estimator, one needs to call `Pipeline.fit(TableEnvironment, Table)`
    before use the pipeline as a Transformer. In this case the Pipeline is an Estimator and
    can produce a Pipeline as a `Model`.

    If a Pipeline has noEstimator, it is a Transformer and can be applied to a Table directly.
    In this case, `Pipeline#fit(TableEnvironment, Table)` will simply return the pipeline itself.


    In addition, a pipeline can also be used as a PipelineStage in another pipeline, just like an
    ordinaryEstimator or Transformer as describe above.
    """

    def __init__(self, stages=None, pipeline_json=None):
        super().__init__()
        self.stages = []
        self.last_estimator_index = -1
        if stages is not None:
            for stage in stages:
                self.append_stage(stage)
        if pipeline_json is not None:
            self.load_json(pipeline_json)

    def need_fit(self):
        return self.last_estimator_index >= 0

    @staticmethod
    def _is_stage_need_fit(stage):
        return (isinstance(stage, Pipeline) and stage.need_fit()) or \
               ((not isinstance(stage, Pipeline)) and isinstance(stage, Estimator))

    def get_stages(self) -> tuple:
        # make it immutable by changing to tuple
        return tuple(self.stages)

    def append_stage(self, stage: PipelineStage) -> 'Pipeline':
        if self._is_stage_need_fit(stage):
            self.last_estimator_index = len(self.stages)
        elif not isinstance(stage, Transformer):
            raise RuntimeError("All PipelineStages should be Estimator or Transformer!")
        self.stages.append(stage)
        return self

    def fit(self, t_env: TableEnvironment, input: Table) -> 'Pipeline':
        """
        Train the pipeline to fit on the records in the given Table.

        :param t_env: the table environment to which the input table is bound.
        :param input: the table with records to train the Pipeline.
        :returns: a pipeline with same stages as this Pipeline except all Estimators \
        replaced with their corresponding Models.
        """
        transform_stages = []
        for i in range(0, len(self.stages)):
            s = self.stages[i]
            if i <= self.last_estimator_index:
                need_fit = self._is_stage_need_fit(s)
                if need_fit:
                    t = s.fit(t_env, input)
                else:
                    t = s
                transform_stages.append(t)
                input = t.transform(t_env, input)
            else:
                transform_stages.append(s)
        return Pipeline(transform_stages)

    def transform(self, t_env: TableEnvironment, input: Table) -> Table:
        """
        Generate a result table by applying all the stages in this pipeline to
        the input table in order.

        :param t_env: the table environment to which the input table is bound.
        :param input: the table to be transformed.
        :returns: a result table with all the stages applied to the input tables in order.
        """
        if self.need_fit():
            raise RuntimeError("Pipeline contains Estimator, need to fit first.")
        for s in self.stages:
            input = s.transform(t_env, input)
        return input

    def to_json(self) -> str:
        import jsonpickle
        return str(jsonpickle.encode(self, keys=True))

    def load_json(self, json: str) -> None:
        import jsonpickle
        pipeline = jsonpickle.decode(json, keys=True)
        for stage in pipeline.get_stages():
            self.append_stage(stage)