import logging
import os

from .cross_account.approve_share import (
    CrossAccountShareApproval,
)
from .cross_account.revoke_share import (
    CrossAccountShareRevoke,
)
from .same_account.approve_share import (
    SameAccountShareApproval,
)
from .same_account.revoke_share import SameAccountShareRevoke
from ...aws.handlers.lakeformation import LakeFormation
from ...aws.handlers.ram import Ram
from ...aws.handlers.sts import SessionHelper
from ...db import api, models, Engine
from ...utils import Parameter

log = logging.getLogger(__name__)


class DataSharingService:
    def __init__(self):
        pass

    @classmethod
    def approve_share(cls, engine: Engine, share_uri: str) -> bool:
        """
        1) Retrieves share related model objects
        2) Build shared database name (unique db per team for a dataset)
        3) Grants pivot role ALL permissions on dataset db and its tables
        4) Calls sharing approval service
        Parameters
        ----------
        engine : db.engine
        share_uri : share uri

        Returns
        -------
        True if approve succeeds
        """
        with engine.scoped_session() as session:
            (
                env_group,
                dataset,
                share,
                shared_tables,
                source_environment,
                target_environment,
            ) = api.ShareObject.get_share_data(session, share_uri, [models.Enums.ShareObjectStatus.Approved.value])

        shared_db_name = cls.build_shared_db_name(dataset, share)

        LakeFormation.grant_pivot_role_all_database_permissions(
            source_environment.AwsAccountId,
            source_environment.region,
            dataset.GlueDatabaseName,
        )

        if source_environment.AwsAccountId != target_environment.AwsAccountId:
            return CrossAccountShareApproval(
                session,
                shared_db_name,
                dataset,
                share,
                shared_tables,
                source_environment,
                target_environment,
                env_group,
            ).approve_share()

        return SameAccountShareApproval(
            session,
            shared_db_name,
            dataset,
            share,
            shared_tables,
            source_environment,
            target_environment,
            env_group,
        ).approve_share()

    @classmethod
    def reject_share(cls, engine: Engine, share_uri: str):
        """
        1) Retrieves share related model objects
        2) Build shared database name (unique db per team for a dataset)
        3) Grants pivot role ALL permissions on dataset db and its tables
        4) Calls sharing revoke service

        Parameters
        ----------
        engine : db.engine
        share_uri : share uri

        Returns
        -------
        True if reject succeeds
        """

        with engine.scoped_session() as session:
            (
                env_group,
                dataset,
                share,
                shared_tables,
                source_environment,
                target_environment,
            ) = api.ShareObject.get_share_data(session, share_uri, [models.Enums.ShareObjectStatus.Rejected.value])

            log.info(f'Revoking permissions for tables : {shared_tables}')

            shared_db_name = cls.build_shared_db_name(dataset, share)

            LakeFormation.grant_pivot_role_all_database_permissions(
                source_environment.AwsAccountId,
                source_environment.region,
                dataset.GlueDatabaseName,
            )

            if source_environment.AwsAccountId != target_environment.AwsAccountId:
                return CrossAccountShareRevoke(
                    session,
                    shared_db_name,
                    env_group,
                    dataset,
                    share,
                    shared_tables,
                    source_environment,
                    target_environment,
                ).revoke_share()

            return SameAccountShareRevoke(
                session,
                shared_db_name,
                env_group,
                dataset,
                share,
                shared_tables,
                source_environment,
                target_environment,
            ).revoke_share()

    @classmethod
    def build_shared_db_name(
        cls, dataset: models.Dataset, share: models.ShareObject
    ) -> str:
        """
        Build Glue shared database name.
        Unique per share Uri.
        Parameters
        ----------
        dataset : models.Dataset
        share : models.ShareObject

        Returns
        -------
        Shared database name
        """
        return (dataset.GlueDatabaseName + '_shared_' + share.shareUri)[:254]

    @classmethod
    def clean_lfv1_ram_resources(cls, environment: models.Environment):
        """
        Deletes LFV1 resource shares for an environment
        Parameters
        ----------
        environment : models.Environment

        Returns
        -------
        None
        """
        return Ram.delete_lakeformation_v1_resource_shares(
            SessionHelper.remote_session(accountid=environment.AwsAccountId).client(
                'ram', region_name=environment.region
            )
        )

    @classmethod
    def refresh_shares(cls, engine: Engine) -> bool:
        """
        Refreshes the shares at scheduled frequency
        Also cleans up LFV1 ram resource shares if enabled on SSM
        Parameters
        ----------
        engine : db.engine

        Returns
        -------
        true if refresh succeeds
        """
        with engine.scoped_session() as session:
            environments = session.query(models.Environment).all()
            shares = (
                session.query(models.ShareObject)
                .filter(models.ShareObject.status.in_(['Approved', 'Rejected']))
                .all()
            )

        # Feature toggle: default value is False
        if (
            Parameter().get_parameter(
                os.getenv('envname', 'local'), 'shares/cleanlfv1ram'
            )
            == 'True'
        ):
            log.info('LFV1 Cleanup toggle is enabled')
            for e in environments:
                log.info(
                    f'Cleaning LFV1 ram resource for environment: {e.AwsAccountId}/{e.region}...'
                )
                cls.clean_lfv1_ram_resources(e)

        if not shares:
            log.info('No Approved nor Rejected shares found. Nothing to do...')
            return True

        for share in shares:
            try:
                log.info(
                    f'Refreshing share {share.shareUri} with {share.status} status...'
                )
                if share.status == 'Approved':
                    cls.approve_share(engine, share.shareUri)
                elif share.status == 'Rejected':
                    cls.reject_share(engine, share.shareUri)
            except Exception as e:
                log.error(
                    f'Failed refreshing share {share.shareUri} with {share.status}. '
                    f'due to: {e}'
                )
        return True
